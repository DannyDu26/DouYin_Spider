"""离线验证限速、重试预算及共享退避，不访问小红书。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
import threading
import time
from types import SimpleNamespace

import pytest

from xhs_apis.errors import XhsApiError, XhsRiskControlError
from xhs_apis.request_policy import XhsRequestPolicy, retry_after_seconds
from xhs_apis.xhs_pc_apis import XHS_Apis


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def policy(self, **kwargs):
        return XhsRequestPolicy(clock=lambda: self.now, sleep=self.sleep,
                                uniform=lambda low, high: high, **kwargs)


def response(status=200, headers=None, data=None):
    return SimpleNamespace(status_code=status, headers=headers or {},
                           json=lambda: {'success': True, 'data': data or {}})


@pytest.mark.parametrize('value,expected', [('120', 120), ('0', 0), ('-1', None),
                                         ('1.2', None), ('invalid', None), ('', None)])
def test_retry_after_seconds(value, expected):
    assert retry_after_seconds(response(headers={'Retry-After': value}), now=1000) == expected


def test_retry_after_http_date_and_expired_date():
    date = format_datetime(datetime.fromtimestamp(1100, timezone.utc), usegmt=True)
    assert retry_after_seconds(response(headers={'retry-after': date}), now=1000) == 100
    assert retry_after_seconds(response(headers={'Retry-After': date}), now=1200) == 0


def test_shared_pacing_waits_before_signing_across_clients_and_interfaces(monkeypatch):
    clock = FakeClock()
    policy = clock.policy(interval=5, jitter=5)
    signed_at, sent_at = [], []

    def sign(self, *args):
        signed_at.append(clock.now)
        return {}, {}, ''

    def send(*args, **kwargs):
        sent_at.append(clock.now)
        clock.now += 2
        return response(data={'user_id': 'user', 'guest': False, 'items': []})

    # 两个客户端模拟不同账号，共享服务端策略，首次详情还需先校验登录。
    def auth():
        state = SimpleNamespace(user_id='', proxies=None, http_client=SimpleNamespace(request=send),
                                origin=lambda kind: 'https://example.test', merge_response_cookies=lambda res: None)
        state.set_user_id = lambda value: setattr(state, 'user_id', value)
        return state

    monkeypatch.setattr(XHS_Apis, '_request_params', sign)
    first, second = XHS_Apis(auth(), policy), XHS_Apis(auth(), policy)
    first.get_note_info('https://www.xiaohongshu.com/explore/' + 'a' * 24)
    second.get_user_info('b' * 24)
    assert signed_at == sent_at == [100, 112, 124]


def test_503_retries_regenerate_signature_then_succeed(monkeypatch):
    clock = FakeClock()
    policy = clock.policy(interval=1, jitter=0, backoff=3)
    replies = iter([response(503), response(503), response(data={'ok': True})])
    signed_at = []
    auth = SimpleNamespace(user_id='user', proxies=None, origin=lambda kind: 'https://example.test',
                           http_client=SimpleNamespace(request=lambda *args, **kwargs: next(replies)),
                           merge_response_cookies=lambda res: None)

    def sign(self, *args):
        signed_at.append(clock.now)
        return {}, {}, ''

    monkeypatch.setattr(XHS_Apis, '_request_params', sign)
    assert XHS_Apis(auth, policy).get_user_info('user') == {'ok': True}
    assert signed_at == [100, 103, 109]
    # 恢复成功后仍遵守普通请求间隔。
    assert policy.execute(lambda: response()).status_code == 200
    assert clock.now == 110


def test_503_exhaustion_blocks_new_requests_and_allows_one_probe():
    clock = FakeClock()
    policy = clock.policy(interval=0, jitter=0, backoff=2)
    calls = []

    def fail():
        calls.append(clock.now)
        return response(503)

    with pytest.raises(XhsApiError) as caught:
        policy.execute(fail)
    assert calls == [100, 102, 106]
    assert caught.value.code == 'XHS_UPSTREAM_UNAVAILABLE'
    assert caught.value.details['retry_after_seconds'] == 8
    with pytest.raises(XhsApiError):
        policy.execute(fail)
    assert len(calls) == 3
    clock.sleep(8)
    assert policy.execute(lambda: response()).status_code == 200
    # 探测成功后退避计数清零。
    policy.retries = 0
    with pytest.raises(XhsApiError) as recovered:
        policy.execute(fail)
    assert recovered.value.details['retry_after_seconds'] == 2


def test_long_retry_after_does_not_sleep_or_hit_upstream_again():
    clock = FakeClock()
    policy = clock.policy(interval=0, jitter=0)
    calls = []

    def fail():
        calls.append(1)
        return response(503, {'Retry-After': '3600'})

    for _ in range(2):
        with pytest.raises(XhsApiError) as caught:
            policy.execute(fail)
        assert caught.value.details['retry_after_seconds'] == 3600
    assert calls == [1] and not clock.waits


def test_failed_recovery_probe_does_not_burst_retries():
    clock = FakeClock()
    policy = clock.policy(interval=0, jitter=0, backoff=2, retries=0)
    calls = []

    def fail():
        calls.append(clock.now)
        return response(503)

    with pytest.raises(XhsApiError):
        policy.execute(fail)
    clock.sleep(2)
    policy.retries = 2
    with pytest.raises(XhsApiError) as caught:
        policy.execute(fail)
    assert calls == [100, 102]
    assert caught.value.details['retry_after_seconds'] == 4


def test_short_retry_after_is_honored_within_budget():
    clock = FakeClock()
    policy = clock.policy(interval=0, jitter=0, backoff=2)
    replies = iter([response(503, {'Retry-After': '12'}), response()])
    assert policy.execute(lambda: next(replies)).status_code == 200
    assert clock.waits == [12]


def test_retry_wait_budget_is_cumulative():
    clock = FakeClock()
    policy = clock.policy(interval=0, jitter=0, acquire_timeout=7, backoff=3)
    calls = []

    def fail():
        calls.append(clock.now)
        return response(503)

    with pytest.raises(XhsApiError):
        policy.execute(fail)
    assert calls == [100, 103]
    assert clock.waits == [3]


@pytest.mark.parametrize('status', [401, 429, 461, 471, 500, 502])
def test_other_errors_are_never_automatically_retried(status):
    clock = FakeClock()
    calls = []

    def send():
        calls.append(1)
        return response(status)

    assert clock.policy().execute(send).status_code == status
    assert calls == [1] and not clock.waits


def test_429_keeps_server_wait_hint():
    with pytest.raises(XhsRiskControlError) as caught:
        XHS_Apis.decode_response(response(429, {'Retry-After': '1800'}))
    assert caught.value.details == {'signal': 'http_429', 'retry_after_seconds': 1800}


def test_concurrent_requests_queue_without_overlapping():
    policy = XhsRequestPolicy(interval=0.01, jitter=0, acquire_timeout=2)
    entered, release = threading.Event(), threading.Event()
    times = []

    def first():
        entered.set()
        assert release.wait(2)
        times.append(time.monotonic())
        return response()

    def second():
        times.append(time.monotonic())
        return response()

    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(policy.execute, first)
        assert entered.wait(2)
        two = executor.submit(policy.execute, second)
        assert not two.done()
        release.set()
        assert one.result(timeout=3).status_code == two.result(timeout=3).status_code == 200
    assert times[1] - times[0] >= 0.01


def test_lock_queue_timeout_never_sends_and_releases_cleanly():
    policy = XhsRequestPolicy(interval=0, jitter=0, acquire_timeout=0.01)
    calls = []
    policy._lock.acquire()
    try:
        with pytest.raises(XhsApiError) as caught:
            policy.execute(lambda: calls.append(1))
        assert caught.value.code == 'XHS_REQUEST_QUEUE_FULL'
        assert not calls
    finally:
        policy._lock.release()
    assert policy.execute(lambda: response()).status_code == 200


@pytest.mark.parametrize('key,value', [
    ('XHS_REQUEST_INTERVAL_SECONDS', 'nan'), ('XHS_REQUEST_JITTER_SECONDS', 'inf'),
    ('XHS_REQUEST_QUEUE_TIMEOUT_SECONDS', '0'), ('XHS_UPSTREAM_MAX_RETRIES', '11'),
    ('XHS_UPSTREAM_MAX_RETRIES', '1.5'), ('XHS_UPSTREAM_BACKOFF_SECONDS', '-1'),
    ('XHS_UPSTREAM_BACKOFF_MAX_SECONDS', '0'),
])
def test_invalid_configuration_rejected(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises((RuntimeError, ValueError)):
        XhsRequestPolicy.from_env()

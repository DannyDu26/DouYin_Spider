# coding=utf-8
import asyncio
import base64
import threading
import time

import pytest
from fastapi.testclient import TestClient

import main
from account_pool import AccountPool
from account_store import CredentialRecord
from builder.auth import DouyinAuth
from dy_apis.login_api import BrowserVerificationRequiredError, DYLoginApi
from qr_login_service import QrLoginService
from spider_service import SpiderService


@pytest.fixture(autouse=True)
def api_environment(monkeypatch):
    """扫码 API 测试不读取开发机真实环境文件。"""
    monkeypatch.setattr(main, 'load_environment', lambda: None)
    monkeypatch.setattr(main, 'get_app_env', lambda: 'dev')
    monkeypatch.delenv('QR_LOGIN_HEADLESS', raising=False)


def make_auth(label='fresh') -> DouyinAuth:
    auth = DouyinAuth()
    auth.perepare_auth(
        f'sessionid={label}; s_v_web_id=fp-{label}; ttwid=tw-{label}',
        '',
        '',
    )
    auth.ticket = f'ticket-{label}'
    auth.ts_sign = f'sign-{label}'
    auth.client_cert = f'cert-{label}'
    auth.private_key = f'private-{label}'
    return auth


class MemoryStore:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.calls = []

    def insert(self, account_id, payload):
        if self.should_fail:
            raise RuntimeError('database-password-must-not-leak')
        self.calls.append((account_id, payload))
        return CredentialRecord(
            row_id=len(self.calls),
            account_id=account_id,
            created_at=None,
            auth=None,
        )


class BlockingStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def insert(self, account_id, payload):
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError('测试未释放数据库写入')
        try:
            return super().insert(account_id, payload)
        finally:
            self.finished.set()


class SuccessfulLoginAPI:
    async def login_grab_ticket(self, **kwargs):
        await kwargs['qrcode_callback'](b'fake-png-bytes')
        # 保证创建接口先返回 waiting_scan
        import asyncio
        await asyncio.sleep(0.05)
        return make_auth()


class SlowLoginAPI:
    async def login_grab_ticket(self, **kwargs):
        await kwargs['qrcode_callback'](b'slow-png')
        import asyncio
        await asyncio.sleep(60)
        return make_auth('slow')


class VerificationRequiredLoginAPI:
    async def login_grab_ticket(self, **kwargs):
        raise BrowserVerificationRequiredError('sensitive verification detail')


def wait_for_status(client, session_id, expected, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f'/api/v1/douyin/auth/qr-sessions/{session_id}')
        if response.json()['data']['status'] == expected:
            return response
        time.sleep(0.02)
    raise AssertionError(f'扫码会话未进入状态: {expected}')


def make_client(login_api, store=None, persistence_timeout_seconds=30):
    store = store or MemoryStore()
    pool = AccountPool()
    spider = SpiderService(account_pool=pool)
    qr_service = QrLoginService(
        store,
        pool,
        login_api=login_api,
        session_timeout_seconds=5,
        terminal_retention_seconds=30,
        qr_ready_timeout_seconds=1,
        persistence_timeout_seconds=persistence_timeout_seconds,
    )
    return TestClient(main.create_app(spider, qr_service)), store, pool


def test_qr_login_returns_base64_then_activates_account():
    client, store, pool = make_client(SuccessfulLoginAPI())
    with client:
        created = client.post('/api/v1/douyin/auth/qr-sessions', json={'account_id': 'marketing-01'})
        body = created.json()['data']
        assert created.status_code == 201
        assert body['status'] == 'waiting_scan'
        encoded = body['qrcode_data_url'].split(',', 1)[1]
        assert base64.b64decode(encoded) == b'fake-png-bytes'
        assert 'ticket-fresh' not in created.text
        assert 'private-fresh' not in created.text

        wait_for_status(client, body['session_id'], 'succeeded')
        accounts = client.get('/api/v1/douyin/auth/accounts').json()['data']['items']

    assert store.calls[0][0] == 'marketing-01'
    assert accounts[0]['account_id'] == 'marketing-01'
    assert pool.stats()['available'] == 1


def test_only_one_qr_session_can_be_active_and_it_can_be_cancelled():
    client, _, _ = make_client(SlowLoginAPI())
    with client:
        first = client.post('/api/v1/douyin/auth/qr-sessions', json={'account_id': 'account-a'})
        session_id = first.json()['data']['session_id']
        second = client.post('/api/v1/douyin/auth/qr-sessions', json={'account_id': 'account-b'})
        cancelled = client.delete(f'/api/v1/douyin/auth/qr-sessions/{session_id}')

    assert second.status_code == 409
    assert second.json()['error']['code'] == 'QR_SESSION_ACTIVE'
    assert cancelled.json()['data']['status'] == 'cancelled'


def test_database_failure_does_not_activate_new_account():
    store = MemoryStore(should_fail=True)
    client, _, pool = make_client(SuccessfulLoginAPI(), store)
    with client:
        created = client.post('/api/v1/douyin/auth/qr-sessions', json={'account_id': 'account-a'})
        session_id = created.json()['data']['session_id']
        failed = wait_for_status(client, session_id, 'failed')

    assert failed.json()['data']['error']['code'] == 'QR_LOGIN_FAILED'
    assert 'database-password-must-not-leak' not in failed.text
    assert pool.stats()['total'] == 0


def test_headless_verification_returns_specific_safe_error():
    client, _, pool = make_client(VerificationRequiredLoginAPI())
    with client:
        response = client.post('/api/v1/douyin/auth/qr-sessions', json={
            'account_id': 'account-a',
        })

    assert response.status_code == 502
    assert response.json()['error']['code'] == 'QR_VERIFICATION_REQUIRED'
    assert 'sensitive verification detail' not in response.text
    assert pool.stats()['total'] == 0


def test_session_cannot_be_cancelled_after_database_commit_starts():
    store = BlockingStore()
    client, _, pool = make_client(SuccessfulLoginAPI(), store)
    with client:
        created = client.post('/api/v1/douyin/auth/qr-sessions', json={'account_id': 'account-a'})
        session_id = created.json()['data']['session_id']
        assert store.started.wait(timeout=2)

        cancelled = client.delete(f'/api/v1/douyin/auth/qr-sessions/{session_id}')
        assert cancelled.status_code == 409
        assert cancelled.json()['error']['code'] == 'QR_SESSION_COMMITTING'

        store.release.set()
        wait_for_status(client, session_id, 'succeeded')

    assert len(store.calls) == 1
    assert pool.stats()['available'] == 1


def test_slow_insert_stays_committing_until_database_result_is_known():
    store = BlockingStore()
    client, _, pool = make_client(
        SuccessfulLoginAPI(),
        store,
        persistence_timeout_seconds=0.05,
    )
    with client:
        created = client.post('/api/v1/douyin/auth/qr-sessions', json={'account_id': 'account-a'})
        session_id = created.json()['data']['session_id']
        assert store.started.wait(timeout=2)

        committing = wait_for_status(client, session_id, 'committing')
        assert 'error' not in committing.json()['data']
        assert pool.stats()['total'] == 0
        time.sleep(0.08)
        assert client.get(
            f'/api/v1/douyin/auth/qr-sessions/{session_id}'
        ).json()['data']['status'] == 'committing'

        # 未知事务结果不能释放会话或接受下一次扫码。
        second = client.post('/api/v1/douyin/auth/qr-sessions', json={'account_id': 'account-b'})
        assert second.status_code == 409
        assert second.json()['error']['code'] == 'QR_SESSION_ACTIVE'

        # INSERT 完成后数据库和内存账号池一起进入成功状态。
        store.release.set()
        assert store.finished.wait(timeout=2)
        wait_for_status(client, session_id, 'succeeded')
        assert pool.stats()['available'] == 1

        second = client.post('/api/v1/douyin/auth/qr-sessions', json={'account_id': 'account-b'})
        assert second.status_code == 201
        wait_for_status(client, second.json()['data']['session_id'], 'succeeded')

    accounts = pool.list_accounts()
    assert [item['account_id'] for item in accounts] == ['account-a', 'account-b']


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', 'invalid'])
def test_persistence_timeout_env_must_be_positive(monkeypatch, value):
    monkeypatch.setenv('QR_PERSIST_TIMEOUT_SECONDS', value)

    with pytest.raises(RuntimeError, match='QR_PERSIST_TIMEOUT_SECONDS'):
        QrLoginService(MemoryStore(), AccountPool(), login_api=SuccessfulLoginAPI())


def test_qr_login_headless_is_loaded_from_environment(monkeypatch):
    monkeypatch.setenv('QR_LOGIN_HEADLESS', 'false')
    service = QrLoginService(
        MemoryStore(),
        AccountPool(),
        login_api=SuccessfulLoginAPI(),
        session_timeout_seconds=12,
    )

    assert service.headless is False
    # 可视模式给人工验证码保留完整会话时间。
    assert service.qr_ready_timeout_seconds == 12


@pytest.mark.parametrize('value', ['false', '0', 'no', 'off'])
def test_qr_login_headless_accepts_false_values(monkeypatch, value):
    monkeypatch.setenv('QR_LOGIN_HEADLESS', value)

    service = QrLoginService(
        MemoryStore(),
        AccountPool(),
        login_api=SuccessfulLoginAPI(),
    )

    assert service.headless is False


def test_qr_login_headless_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv('QR_LOGIN_HEADLESS', 'sometimes')

    with pytest.raises(RuntimeError, match='QR_LOGIN_HEADLESS'):
        QrLoginService(MemoryStore(), AccountPool(), login_api=SuccessfulLoginAPI())


def test_visible_login_waits_for_manual_verification_then_clicks():
    class FakePage:
        def __init__(self):
            self.titles = ['验证码中间页', '抖音']
            self.evaluate_calls = 0

        def is_closed(self):
            return False

        async def title(self):
            return self.titles.pop(0)

        async def evaluate(self, script):
            self.evaluate_calls += 1
            return True

    page = FakePage()
    asyncio.run(DYLoginApi.wait_and_click_login(
        page,
        time.time() + 1,
        headless=False,
        poll_interval=0,
    ))

    assert page.evaluate_calls == 1


def test_headless_login_fails_fast_on_verification_page():
    class FakePage:
        def is_closed(self):
            return False

        async def title(self):
            return '验证码中间页'

    with pytest.raises(BrowserVerificationRequiredError):
        asyncio.run(DYLoginApi.wait_and_click_login(
            FakePage(),
            time.time() + 1,
            headless=True,
            poll_interval=0,
        ))


def test_invalid_account_id_is_rejected():
    client, _, _ = make_client(SuccessfulLoginAPI())
    with client:
        response = client.post('/api/v1/douyin/auth/qr-sessions', json={'account_id': '中文账号'})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_REQUEST'

"""抖音 Cookie 同步集成测试，使用内存数据库并禁止真实上游请求。"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import main
from app.account_pool import AccountPool
from app.browser_cookie_import import BrowserCookieImportError
from app.douyin_cookie_import import DouyinCookieImportRequest, DouyinCookieImportService
from app.spider_service import SpiderService
from builder.params import Params
from dy_apis.douyin_api import DouyinAPI, DouyinAuthenticationError, DouyinRequestRejectedError, DouyinRiskControlError
from test_account_pool import make_store
from test_douyin_response import FakeResponse


ROUTE = '/api/v1/douyin/auth/cookie/import'
COOKIE = 'sessionid=fixture-douyin-session; s_v_web_id=fixture-fingerprint; ttwid=fixture-ttwid'
TOKEN = 'fixture-douyin-import-key'


@pytest.fixture
def importer(monkeypatch):
    monkeypatch.setattr(main, 'load_environment', lambda: None)
    monkeypatch.setattr(main, 'get_app_env', lambda: 'dev')
    monkeypatch.setenv('DOUYIN_COOKIE_IMPORT_TOKEN', TOKEN)
    monkeypatch.setattr(DouyinAPI, 'get_my_uid', staticmethod(lambda auth: 123456789))
    store, pool = make_store(), AccountPool()
    try:
        with TestClient(main.create_app(service=SpiderService(account_pool=pool), credential_store=store)) as client:
            yield client, store, pool
    finally:
        pool.close()
        store.close()


def upload(client, cookie=COOKIE, token=TOKEN):
    return client.post(ROUTE, headers={'X-Douyin-Import-Token': token},
                       json={'account_id': 'douyin_browser', 'cookie': cookie})


def test_import_saves_then_updates_same_douyin_account(importer):
    client, store, pool = importer
    first = upload(client)
    assert first.status_code == 200, first.text
    assert first.json()['data'] == {'account_id': 'douyin_browser', 'status': 'succeeded'}
    assert 'fixture-douyin-session' not in first.text
    assert pool.stats()['available'] == 1
    second = upload(client, COOKIE.replace('fixture-douyin-session', 'fresh-session'))
    assert second.status_code == 200
    records = store.load_latest()
    assert len(records) == 1 and records[0].auth.cookie['sessionid'] == 'fresh-session'
    with pool.acquire() as lease:
        assert lease.auth.uid == 123456789 and lease.auth.cookie['sessionid'] == 'fresh-session'


@pytest.mark.parametrize('token', ['', 'wrong-key'])
def test_bad_token_never_checks_upstream_or_saves(importer, monkeypatch, token):
    def forbidden(auth):
        pytest.fail('鉴权失败不能访问上游')
    monkeypatch.setattr(DouyinAPI, 'get_my_uid', forbidden)
    response = upload(importer[0], token=token)
    assert response.status_code == 401
    assert importer[1].load_latest() == []


def test_platform_tokens_are_separate_and_default_disabled(importer, monkeypatch):
    response = importer[0].post(ROUTE, headers={'X-XHS-Import-Token': TOKEN},
                                json={'account_id': 'douyin_browser', 'cookie': COOKIE})
    assert response.status_code == 401
    monkeypatch.delenv('DOUYIN_COOKIE_IMPORT_TOKEN')
    assert upload(importer[0]).status_code == 503


@pytest.mark.parametrize('cookie', ['s_v_web_id=fp', 'sessionid=secret', COOKIE + '\r\nsecret',
                                    COOKIE + '; sessionid=duplicate', COOKIE + '; invalid'])
def test_invalid_cookie_rejected_without_echo(importer, cookie):
    response = upload(importer[0], cookie)
    assert response.status_code == 422 and 'fixture-douyin-session' not in response.text
    assert importer[1].load_latest() == []


@pytest.mark.parametrize('failure,status,code', [
    (DouyinAuthenticationError('secret-detail'), 401, 'DOUYIN_AUTH_EXPIRED'),
    (DouyinRiskControlError('secret-detail'), 429, 'DOUYIN_VERIFICATION_REQUIRED'),
    (RuntimeError('secret-detail'), 502, 'DOUYIN_COOKIE_VERIFY_FAILED'),
])
def test_failed_verification_preserves_old_credentials(importer, monkeypatch, failure, status, code):
    client, store, pool = importer
    assert upload(client).status_code == 200
    def fail(auth):
        raise failure
    monkeypatch.setattr(DouyinAPI, 'get_my_uid', fail)
    response = upload(client, COOKIE.replace('fixture-douyin-session', 'bad-session'))
    assert response.status_code == status and response.json()['error']['code'] == code
    assert 'secret-detail' not in response.text and 'bad-session' not in response.text
    assert store.load_latest()[0].auth.cookie['sessionid'] == 'fixture-douyin-session'
    assert pool.stats()['available'] == 1


def test_persistence_failure_never_adds_account(importer, monkeypatch):
    def fail(*args):
        raise RuntimeError('secret-database-password')
    monkeypatch.setattr(importer[1], 'insert', fail)
    response = upload(importer[0])
    assert response.status_code == 502
    assert response.json()['error']['code'] == 'DOUYIN_CREDENTIAL_SAVE_FAILED'
    assert 'secret-database-password' not in response.text and importer[2].stats()['total'] == 0


def test_same_account_concurrent_sync_rejected_and_released(monkeypatch):
    store, pool = make_store(), AccountPool()
    service = DouyinCookieImportService(store, pool)
    started, release = threading.Event(), threading.Event()
    payload = DouyinCookieImportRequest(account_id='same', cookie=COOKIE)
    def verify(auth):
        started.set()
        assert release.wait(5)
        return 123
    monkeypatch.setattr(DouyinAPI, 'get_my_uid', verify)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(service.import_cookie, payload)
            try:
                assert started.wait(5)
                with pytest.raises(BrowserCookieImportError) as caught:
                    service.import_cookie(payload)
                assert caught.value.status_code == 409
            finally:
                release.set()
            assert task.result()['status'] == 'succeeded'
        assert service.import_cookie(payload)['status'] == 'succeeded'
    finally:
        pool.close()
        store.close()


@pytest.mark.parametrize('payload,error', [
    ({'status_code': 0, 'user_uid': '123'}, None),
    ({'status_code': 0, 'user_uid': 0}, DouyinAuthenticationError),
    ({'status_code': 0, 'user_uid': True}, DouyinAuthenticationError),
    ({'status_code': 0}, DouyinAuthenticationError),
    ({'status_code': 1, 'user_uid': '123'}, DouyinRequestRejectedError),
    ({'status_code': 1, 'status_msg': '未登录'}, DouyinAuthenticationError),
])
def test_current_user_api_requires_formal_uid(monkeypatch, payload, error):
    # 跳过仅与签名相关的准备，真实执行响应分类和用户 ID 校验。
    monkeypatch.setattr(Params, 'with_web_id', lambda self, *args, **kwargs: self)
    monkeypatch.setattr(Params, 'with_a_bogus', lambda self, *args, **kwargs: self)
    from app.account_store import deserialize_credential
    auth = deserialize_credential(COOKIE)
    def request(*args, **kwargs):
        assert kwargs['timeout'][0] > 0
        assert kwargs['verify'] is not False
        return FakeResponse(payload=payload)
    monkeypatch.setattr(DouyinAPI, '_request', request)
    if error:
        with pytest.raises(error):
            DouyinAPI.get_my_uid(auth)
    else:
        assert DouyinAPI.get_my_uid(auth) == 123

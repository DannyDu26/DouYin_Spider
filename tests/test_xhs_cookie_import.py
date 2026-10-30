"""浏览器 Cookie 导入的鉴权、登录校验和持久化测试，禁止真实网络请求。"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from app.account_pool import AccountPool
from app.account_store import CredentialStoreError
from app.xhs_login_service import XhsLoginService
from app.xhs_service import XhsService
from test_xhs_api import USER_ID
from test_xhs_credentials import make_store
from xhs_apis.xhs_pc_apis import XHS_Apis
from xhs_utils.xhs_pc import XHSPcAuth


ROUTE = '/api/v1/xhs/auth/cookie/import'
TOKEN = 'fixture-import-token'
COOKIE = 'a1=fixture-device; web_session=fixture-secret-session; webBuild=6.47.2'


@pytest.fixture
def importer(monkeypatch):
    monkeypatch.setattr(main, 'load_environment', lambda: None)
    monkeypatch.setattr(main, 'get_app_env', lambda: 'dev')
    monkeypatch.setenv('XHS_COOKIE_IMPORT_TOKEN', TOKEN)
    state = SimpleNamespace(user={'guest': False, 'user_id': USER_ID}, seen=[], closed=[])

    def user_me(client):
        state.seen.append(client.auth)
        return state.user

    original_close = XHSPcAuth.close

    def close(auth):
        state.closed.append(auth)
        original_close(auth)

    monkeypatch.setattr(XHS_Apis, 'get_user_me', user_me)
    monkeypatch.setattr(XHSPcAuth, 'close', close)
    store = make_store()
    pool = AccountPool(manage_auth_resources=True, cooldown_failure_limit=0)
    login = XhsLoginService(store, pool)
    douyin = SimpleNamespace(test_account_pinning_enabled=False, max_concurrent=1,
                             account_pool=AccountPool(), account_stats=lambda: {'total': 0, 'available': 0})
    app = main.create_app(service=douyin, xhs_service=XhsService(pool), xhs_login_service=login)
    try:
        with TestClient(app) as client:
            yield client, store, pool, login, state
    finally:
        pool.close()
        store.close()


def upload(client, cookie=COOKIE, token=TOKEN):
    return client.post(ROUTE, headers={'X-XHS-Import-Token': token},
                       json={'account_id': 'browser_one', 'cookie': cookie})


def test_import_validates_saves_and_updates_same_account(importer):
    client, store, pool, _, state = importer
    first = upload(client)
    assert first.status_code == 200, first.text
    assert first.json()['data']['status'] == 'succeeded'
    assert COOKIE not in first.text and 'fixture-secret-session' not in first.text
    assert pool.stats()['available'] == 1
    # 同一别名再次同步，更新已有凭证而非新增账号。
    second = upload(client, COOKIE.replace('fixture-secret-session', 'refreshed-session'))
    assert second.status_code == 200
    records = store.load_latest()
    try:
        assert len(records) == 1 and pool.stats()['total'] == 1
        assert 'web_session=refreshed-session' in records[0].stored_cookie
        assert records[0].auth.login_source == 'cookie'
        assert state.seen[-1].user_id == USER_ID and state.seen[-1].login_verified
    finally:
        for record in records:
            record.auth.close()


@pytest.mark.parametrize('token', ['', 'wrong-token', '非ASCII密钥'])
def test_invalid_token_never_validates_or_saves(importer, token):
    # HTTP 头只支持可编码字符；非 ASCII 比较通过直接调用依赖验证。
    client, store, pool, _, state = importer
    if not token.isascii():
        from app.xhs_router import require_cookie_import_token
        from xhs_apis.errors import XhsApiError
        with pytest.raises(XhsApiError) as caught:
            require_cookie_import_token(token)
        assert caught.value.status_code == 401
    else:
        response = upload(client, token=token)
        assert response.status_code == 401
        assert 'fixture-secret-session' not in response.text
    assert not state.seen and store.load_latest() == [] and pool.stats()['total'] == 0


def test_import_disabled_without_server_token(importer, monkeypatch):
    monkeypatch.delenv('XHS_COOKIE_IMPORT_TOKEN')
    response = upload(importer[0])
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'XHS_COOKIE_IMPORT_DISABLED'
    assert not importer[4].seen


@pytest.mark.parametrize('cookie', [
    'a1=fixture', 'web_session=fixture-secret-session',
    COOKIE + '\r\nsecret', COOKIE + '; web_session=duplicate', COOKIE + '; invalid',
])
def test_invalid_cookie_is_rejected_without_echo(importer, cookie):
    response = upload(importer[0], cookie)
    assert response.status_code == 422
    assert 'fixture-secret-session' not in response.text
    assert not importer[4].seen


def test_guest_never_overwrites_existing_account_and_closes_client(importer):
    client, store, pool, _, state = importer
    assert upload(client).status_code == 200
    state.user = {'guest': True, 'user_id': USER_ID}
    response = upload(client, COOKIE.replace('fixture-secret-session', 'guest-session'))
    assert response.status_code == 401
    assert state.seen[-1] in state.closed
    assert pool.stats()['available'] == 1
    records = store.load_latest()
    try:
        assert len(records) == 1 and 'web_session=fixture-secret-session' in records[0].stored_cookie
    finally:
        for record in records:
            record.auth.close()


def test_database_failure_does_not_add_account_and_closes_client(importer, monkeypatch):
    client, _, pool, login, state = importer

    def broken_insert(*args):
        raise CredentialStoreError('fixture-secret-session')

    monkeypatch.setattr(login.store, 'insert', broken_insert)
    response = upload(client)
    assert response.status_code == 502
    assert response.json()['error']['code'] == 'XHS_CREDENTIAL_SAVE_FAILED'
    assert 'fixture-secret-session' not in response.text
    assert state.seen[-1] in state.closed and pool.stats()['total'] == 0


def test_risk_response_closes_import_and_allows_manual_retry(importer, monkeypatch):
    from xhs_apis.errors import XhsRiskControlError
    client, _, pool, _, state = importer

    def risk(client):
        state.seen.append(client.auth)
        raise XhsRiskControlError('http_461', 60)

    monkeypatch.setattr(XHS_Apis, 'get_user_me', risk)
    response = upload(client)
    assert response.status_code == 429 and response.headers['retry-after'] == '60'
    assert state.seen[-1] in state.closed and pool.stats()['total'] == 0
    # 用户在浏览器完成验证后，可以再次点击同步，不被残留会话锁住。
    monkeypatch.setattr(XHS_Apis, 'get_user_me', lambda client: {'guest': False, 'user_id': USER_ID})
    assert upload(client).status_code == 200

"""短信登录状态机与持久化失败测试，禁止调用真实短信接口。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.account_pool import AccountPool
from app.account_store import CredentialStoreError
from app.xhs_login_service import XhsLoginService
from app.xhs_schemas import SmsSessionRequest
from xhs_apis.errors import XhsApiError
from xhs_utils.xhs_core.cookies import HostCookieStore
from xhs_utils.xhs_pc.state import PcDeviceProfile
from test_xhs_api import USER_ID
from test_xhs_credentials import make_store


class FakeLogin:
    def __init__(self):
        self.proxies = None
        self.dsl = '1720000000000'
        self.closed = False
        self.http = SimpleNamespace(close=self.close)
        self._cookie_store = HostCookieStore()

    def close(self):
        self.closed = True

    def generate_init_cookies(self):
        cookies = {'a1': 'fixture', 'web_session': 'anonymous', 'gid': 'gid',
                   'loadts': '1720000000000', 'webBuild': '6.47.2'}
        self.profile = PcDeviceProfile(cookies=cookies)
        # 模拟刚完成安全初始化，禁止离线测试触发真实 DS 请求。
        from xhs_utils.xhs_pc.state import now_ms
        self.profile.session.dsllt = now_ms()
        self.profile.update_storage(local_storage={'webSsk': '{"xhs-pc-web":"fixture-secret"}'})
        return cookies

    def ensure_webprofile(self, cookies):
        return 'gid'

    def send_phone_code(self, phone, cookies, zone):
        return True, '', {'success': True}

    def login_by_phone(self, phone, code, cookies, zone):
        if code != '123456':
            return False, 'upstream-secret-detail', {'cookies': cookies}
        cookies['web_session'] = 'formal-session'
        return True, '', {'cookies': cookies}

    def get_user_info(self, cookies):
        return True, {'guest': False, 'user_id': USER_ID}, cookies

    def host_cookie_state(self):
        return self._cookie_store.export_state()


def run(coroutine):
    asyncio.run(coroutine)


def test_sms_success_persists_formal_cookie_and_ssk():
    async def scenario():
        store = make_store()
        pool = AccountPool(manage_auth_resources=True, cooldown_failure_limit=0)
        service = XhsLoginService(store, pool, FakeLogin)
        created = await service.create_session(SmsSessionRequest(account_id='one', phone='13800000000'))
        session = service.sessions[created['session_id']]
        await session.task
        assert session.status == 'ready'
        await service.send_code(session.session_id)
        await session.task
        assert session.status == 'waiting_sms_code'
        login = session.login
        await service.verify_code(session.session_id, '123456')
        await session.task
        assert session.status == 'succeeded' and session.login is None
        record = store.load_latest()[0]
        assert 'web_session=formal-session' in record.stored_cookie
        assert json.loads(record.stored_remark)['local_storage']['webSsk']
        assert pool.stats()['available'] == 1
        assert not login.closed
        public = json.dumps(await service.get_session(session.session_id))
        assert '13800000000' not in public and 'fixture-secret' not in public
        record.auth.close()
        await service.shutdown()
        pool.close()
        store.close()
    run(scenario())


def test_wrong_code_retry_and_resend_limit():
    async def scenario():
        store = make_store()
        pool = AccountPool(manage_auth_resources=True)
        service = XhsLoginService(store, pool, FakeLogin)
        view = await service.create_session(SmsSessionRequest(account_id='one', phone='13800000000'))
        session = service.sessions[view['session_id']]
        await session.task
        await service.send_code(session.session_id)
        await session.task
        with pytest.raises(XhsApiError) as caught:
            await service.send_code(session.session_id)
        assert caught.value.status_code == 429
        await service.verify_code(session.session_id, '999999')
        await session.task
        assert session.status == 'waiting_sms_code'
        assert session.error_code == 'XHS_SMS_INVALID_CODE'
        assert store.load_latest() == []
        await service.verify_code(session.session_id, '123456')
        await session.task
        assert session.status == 'succeeded'
        await service.shutdown()
        pool.close()
        store.close()
    run(scenario())


def test_database_failure_never_adds_account():
    async def scenario():
        class BrokenStore:
            def insert(self, *args):
                raise CredentialStoreError('fixture database failure')
        pool = AccountPool(manage_auth_resources=True)
        service = XhsLoginService(BrokenStore(), pool, FakeLogin)
        view = await service.create_session(SmsSessionRequest(account_id='one', phone='13800000000'))
        session = service.sessions[view['session_id']]
        await session.task
        await service.send_code(session.session_id)
        await session.task
        login = session.login
        await service.verify_code(session.session_id, '123456')
        await session.task
        assert session.status == 'failed' and login.closed
        assert session.error_code == 'XHS_CREDENTIAL_SAVE_FAILED'
        assert pool.stats()['total'] == 0
        await service.shutdown()
    run(scenario())


def test_conflict_cancel_and_expiry():
    async def scenario():
        pool = AccountPool(manage_auth_resources=True)
        service = XhsLoginService(None, pool, FakeLogin)
        payload = SmsSessionRequest(account_id='one', phone='13800000000')
        view = await service.create_session(payload)
        session = service.sessions[view['session_id']]
        await session.task
        login = session.login
        with pytest.raises(XhsApiError):
            await service.create_session(payload)
        assert (await service.cancel_session(session.session_id))['status'] == 'cancelled'
        assert login.closed
        second = await service.create_session(payload)
        session = service.sessions[second['session_id']]
        await session.task
        session.expires_at = 0
        assert (await service.get_session(session.session_id))['status'] == 'expired'
        with pytest.raises(XhsApiError) as caught:
            await service.send_code(session.session_id)
        assert caught.value.status_code == 409
        await service.shutdown()
    run(scenario())


def test_async_sms_routes_are_removed(monkeypatch):
    """旧接口实际返回 404，而不是仅从文档隐藏。"""
    import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, 'load_environment', lambda: None)
    monkeypatch.setattr(main, 'get_app_env', lambda: 'dev')
    douyin = SimpleNamespace(test_account_pinning_enabled=False, max_concurrent=1,
                             account_pool=AccountPool(), account_stats=lambda: {'total': 0, 'available': 0})
    with TestClient(main.create_app(service=douyin)) as client:
        base = '/api/v1/xhs/auth/sms-sessions'
        for method, path in [('POST', base), ('GET', base + '/session'),
                             ('POST', base + '/session/send'), ('POST', base + '/session/verify'),
                             ('DELETE', base + '/session')]:
            assert client.request(method, path).status_code == 404
        schema = client.get('/api/openapi.json').json()
        assert not any(path.startswith(base) for path in schema['paths'])
        assert 'SmsCodeRequest' not in schema['components']['schemas']


def test_extra_verification_has_explicit_state():
    async def scenario():
        from xhs_apis.errors import XhsRiskControlError
        class RiskLogin(FakeLogin):
            def send_phone_code(self, *args):
                raise XhsRiskControlError('http_461')
        service = XhsLoginService(None, AccountPool(), RiskLogin)
        view = await service.create_session(SmsSessionRequest(account_id='one', phone='13800000000'))
        session = service.sessions[view['session_id']]
        await session.task
        await service.send_code(session.session_id)
        await session.task
        assert session.status == 'verification_required'
        with pytest.raises(XhsApiError):
            await service.verify_code(session.session_id, '123456')
        await service.shutdown()
    run(scenario())


def test_two_request_login_routes(monkeypatch):
    """两次 HTTP 请求完成登录，并验证错误验证码可以直接重试。"""
    import main
    from fastapi.testclient import TestClient
    from app.xhs_service import XhsService
    from test_xhs_api import FakeClient, NOTE_ID
    monkeypatch.setattr(main, 'load_environment', lambda: None)
    monkeypatch.setattr(main, 'get_app_env', lambda: 'dev')
    store = make_store()
    pool = AccountPool(manage_auth_resources=True, cooldown_failure_limit=0)
    login = XhsLoginService(store, pool, FakeLogin)
    douyin = SimpleNamespace(test_account_pinning_enabled=False, max_concurrent=1,
                             account_pool=AccountPool(), account_stats=lambda: {'total': 0, 'available': 0})
    FakeClient.failure = None
    with TestClient(main.create_app(service=douyin, xhs_service=XhsService(pool, store, client_factory=FakeClient),
                                   xhs_login_service=login)) as client:
        payload = {'account_id': 'one', 'phone': '13800000000'}
        sent = client.post('/api/v1/xhs/auth/sms/send', json=payload)
        assert sent.status_code == 200, sent.text
        session_id = sent.json()['data']['session_id']
        assert sent.json()['data']['status'] == 'waiting_sms_code'
        assert '13800000000' not in sent.text
        repeated = client.post('/api/v1/xhs/auth/sms/send', json=payload)
        assert repeated.status_code == 429
        assert repeated.json()['error']['details']['session_id'] == session_id
        assert len(login.sessions) == 1
        wrong = client.post('/api/v1/xhs/auth/sms/login', json={'session_id': session_id, 'code': '999999'})
        assert wrong.status_code == 422
        assert wrong.json()['error']['code'] == 'XHS_SMS_INVALID_CODE'
        assert pool.stats()['total'] == 0
        assert store.load_latest() == []
        logged_in = client.post('/api/v1/xhs/auth/sms/login', json={'session_id': session_id, 'code': '123456'})
        assert logged_in.status_code == 200, logged_in.text
        assert logged_in.json()['data']['status'] == 'succeeded'
        assert pool.stats()['available'] == 1
        assert len(store.load_latest()) == 1
        assert client.post('/api/v1/xhs/note_info', json={'note_id': NOTE_ID, 'xsec_token': 'token'}).status_code == 200
        assert 'formal-session' not in logged_in.text and 'fixture-secret' not in logged_in.text
        assert client.post('/api/v1/xhs/auth/sms/login', json={'session_id': 'missing', 'code': '123456'}).status_code == 404
    store.close()


def test_simple_send_failure_and_resend():
    async def scenario():
        class RetryLogin(FakeLogin):
            sends = 0
            def send_phone_code(self, *args):
                self.sends += 1
                return self.sends > 1, '', {}
        service = XhsLoginService(None, AccountPool(), RetryLogin)
        payload = SmsSessionRequest(account_id='one', phone='13800000000')
        try:
            with pytest.raises(XhsApiError) as caught:
                await service.send_sms(payload)
            assert caught.value.code == 'XHS_SMS_SEND_FAILED'
            session = service.sessions[caught.value.details['session_id']]
            assert session.status == 'ready'
            service._last_send[('86', payload.phone)] -= service.send_interval
            sent = await service.send_sms(payload)
            assert sent['session_id'] == session.session_id
            assert sent['status'] == 'waiting_sms_code'
            assert session.login.sends == 2
            with pytest.raises(XhsApiError) as conflict:
                await service.send_sms(SmsSessionRequest(account_id='another', phone=payload.phone))
            assert conflict.value.status_code == 409
        finally:
            await service.shutdown()
    run(scenario())


@pytest.mark.parametrize('phase', ['initialize', 'send', 'verify'])
def test_simple_flow_reports_risk_without_polling(phase):
    async def scenario():
        from xhs_apis.errors import XhsRiskControlError
        class RiskLogin(FakeLogin):
            def ensure_webprofile(self, cookies):
                if phase == 'initialize':
                    raise XhsRiskControlError('http_461')
                return super().ensure_webprofile(cookies)
            def send_phone_code(self, *args):
                if phase == 'send':
                    raise XhsRiskControlError('http_461')
                return super().send_phone_code(*args)
            def login_by_phone(self, *args):
                if phase == 'verify':
                    raise XhsRiskControlError('http_471')
                return super().login_by_phone(*args)
        service = XhsLoginService(None, AccountPool(), RiskLogin)
        try:
            with pytest.raises(XhsApiError) as caught:
                sent = await service.send_sms(SmsSessionRequest(account_id='one', phone='13800000000'))
                await service.login_sms(sent['session_id'], '123456')
            assert caught.value.status_code == 429
            assert caught.value.details['status'] == 'verification_required'
            assert caught.value.details['signal'] == ('http_471' if phase == 'verify' else 'http_461')
        finally:
            await service.shutdown()
    run(scenario())


def test_simple_login_reports_persistence_failure():
    async def scenario():
        class BrokenStore:
            def insert(self, *args):
                raise CredentialStoreError('database-secret')
        pool = AccountPool(manage_auth_resources=True)
        service = XhsLoginService(BrokenStore(), pool, FakeLogin)
        try:
            sent = await service.send_sms(SmsSessionRequest(account_id='one', phone='13800000000'))
            login = service.sessions[sent['session_id']].login
            with pytest.raises(XhsApiError) as caught:
                await service.login_sms(sent['session_id'], '123456')
            assert caught.value.code == 'XHS_CREDENTIAL_SAVE_FAILED'
            assert caught.value.status_code == 502
            assert caught.value.details['status'] == 'failed'
            assert pool.stats()['total'] == 0 and login.closed
            assert 'database-secret' not in str(caught.value)
        finally:
            await service.shutdown()
    run(scenario())


def test_simple_send_waiter_disconnect_preserves_background_task():
    async def scenario():
        import threading
        entered, release = threading.Event(), threading.Event()
        class SlowLogin(FakeLogin):
            def send_phone_code(self, *args):
                entered.set()
                assert release.wait(5)
                return super().send_phone_code(*args)
        service = XhsLoginService(None, AccountPool(), SlowLogin)
        waiter = asyncio.create_task(service.send_sms(SmsSessionRequest(account_id='one', phone='13800000000')))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            session = next(iter(service.sessions.values()))
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not session.task.cancelled()
            release.set()
            await session.task
            assert session.status == 'waiting_sms_code'
        finally:
            release.set()
            await service.shutdown()
    run(scenario())

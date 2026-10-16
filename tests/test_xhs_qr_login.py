"""小红书纯 HTTP 扫码登录测试，不访问真实上游。"""
import asyncio
import base64
import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main
from app.account_pool import AccountPool
from app.account_store import CredentialStoreError
from app.xhs_login_service import XhsLoginService
from app.xhs_schemas import SmsSessionRequest
from app.xhs_service import XhsService
from test_xhs_api import FakeClient, NOTE_ID
from test_xhs_credentials import make_store
from test_xhs_login import FakeLogin
from xhs_apis.errors import XhsApiError, XhsRiskControlError


class FakeQrLogin(FakeLogin):
    states = ('请扫描二维码', '请确认登录', '验证成功')

    def __init__(self):
        super().__init__()
        self.events = []
        self.polls = 0

    def generate_qrcode(self, cookies):
        self.events.append('create')
        return True, '', {'cookies': cookies, 'qr_id': 'private-qr-id', 'code': 'private-qr-code',
                          'qr_url': 'https://fixture.example/qr?code=fixture'}

    def check_qrcode_status(self, qr_id, code, cookies):
        assert qr_id == 'private-qr-id' and code == 'private-qr-code'
        self.events.append('poll')
        state = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        if state == '验证成功':
            cookies['web_session'] = 'formal-session'
        return state == '验证成功', state, cookies

    def ensure_webprofile(self, cookies):
        self.events.append('profile')
        return super().ensure_webprofile(cookies)


def make_service(factory=FakeQrLogin, store=None):
    pool = AccountPool(manage_auth_resources=True, cooldown_failure_limit=0)
    service = XhsLoginService(store, pool, factory)
    service.qr_poll_interval = 0.001
    return service, pool


def test_qr_routes_persist_then_allow_data_access(monkeypatch):
    """两步扫码返回可展示 PNG，并以正式凭证入池。"""
    monkeypatch.setattr(main, 'load_environment', lambda: None)
    monkeypatch.setattr(main, 'get_app_env', lambda: 'dev')
    store = make_store()
    service, pool = make_service(store=store)
    douyin = SimpleNamespace(test_account_pinning_enabled=False, max_concurrent=1,
                             account_pool=AccountPool(), account_stats=lambda: {'total': 0, 'available': 0})
    FakeClient.failure = None
    with TestClient(main.create_app(service=douyin, xhs_service=XhsService(pool, store, client_factory=FakeClient),
                                   xhs_login_service=service)) as client:
        created = client.post('/api/v1/xhs/auth/qr/create', json={'account_id': 'qr_one'})
        assert created.status_code == 200, created.text
        data = created.json()['data']
        assert data['status'] == 'waiting_scan' and 'phone_masked' not in data
        image = Image.open(BytesIO(base64.b64decode(data['qrcode_data_url'].split(',', 1)[1])))
        assert image.format == 'PNG' and image.width > 100
        session = service.sessions[data['session_id']]
        login = session.login
        assert login.events == ['create', 'poll', 'profile']
        assert session.qrcode_data_url == ''
        assert store.load_latest() == []
        assert client.post('/api/v1/xhs/auth/qr/create', json={'account_id': 'qr_one'}).status_code == 409
        assert client.post('/api/v1/xhs/auth/sms/send', json={'account_id': 'qr_one', 'phone': '13800000000'}).status_code == 409
        assert client.post('/api/v1/xhs/auth/sms/login', json={'session_id': session.session_id, 'code': '123456'}).status_code == 409
        logged_in = client.post('/api/v1/xhs/auth/qr/login', json={'session_id': session.session_id})
        assert logged_in.status_code == 200, logged_in.text
        assert logged_in.json()['data']['status'] == 'succeeded'
        assert 'qrcode_data_url' not in logged_in.text
        assert 'private-qr-code' not in logged_in.text and 'formal-session' not in logged_in.text
        record = store.load_latest()[0]
        assert 'web_session=formal-session' in record.stored_cookie
        state = json.loads(record.stored_remark)
        assert state['login_source'] == 'qrcode' and state['local_storage']['webSsk']
        record.auth.close()
        assert pool.stats()['available'] == 1 and not login.closed
        # 重复确认复用成功结果，防止重复数据库提交。
        polls = login.polls
        assert client.post('/api/v1/xhs/auth/qr/login', json={'session_id': session.session_id}).status_code == 200
        assert login.polls == polls and len(store.load_latest()) == 1
        assert client.post('/api/v1/xhs/note_info', json={'note_id': NOTE_ID, 'xsec_token': 'token'}).status_code == 200
        schema = client.get('/api/openapi.json').json()
        assert schema['paths']['/api/v1/xhs/auth/qr/create']['post']['tags'] == ['小红书 · 登录与账号']
        assert not any('sms-sessions' in path for path in schema['paths'])
        assert client.post('/api/v1/xhs/auth/qr/create', json={'account_id': 'INVALID ID'}).status_code == 422
        assert client.post('/api/v1/xhs/auth/qr/login', json={'session_id': 'missing'}).status_code == 404
    store.close()


@pytest.mark.parametrize('states,code,http_status,status', [
    (('请扫描二维码', '二维码已过期'), 'XHS_QR_EXPIRED', 410, 'expired'),
    (('请扫描二维码', '未知状态: 99'), 'XHS_QR_POLL_FAILED', 502, 'failed'),
    (('请确认登录',), 'XHS_QR_INVALID_INITIAL_STATE', 502, 'failed'),
])
def test_qr_invalid_and_expired_states(states, code, http_status, status):
    async def scenario():
        class InvalidQrLogin(FakeQrLogin):
            pass
        InvalidQrLogin.states = states
        service, pool = make_service(InvalidQrLogin)
        try:
            with pytest.raises(XhsApiError) as caught:
                data = await service.create_qrcode('qr_one')
                await service.login_qrcode(data['session_id'])
            assert caught.value.code == code and caught.value.status_code == http_status
            session = service.sessions[caught.value.details['session_id']]
            assert session.status == status and session.login is None
            assert not session.qr_id and not session.qr_code
            assert pool.stats()['total'] == 0
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['create', 'poll', 'save', 'guest'])
def test_qr_failures_never_enter_pool(phase):
    async def scenario():
        class BrokenStore:
            def insert(self, *args):
                raise CredentialStoreError('private-database-detail')
        class FailureQrLogin(FakeQrLogin):
            def generate_qrcode(self, cookies):
                if phase == 'create':
                    return False, 'private-upstream-detail', None
                return super().generate_qrcode(cookies)
            def check_qrcode_status(self, *args):
                if phase == 'poll' and self.polls > 0:
                    raise XhsRiskControlError('http_471')
                return super().check_qrcode_status(*args)
            def get_user_info(self, cookies):
                if phase == 'guest':
                    return True, {'guest': True}, cookies
                return super().get_user_info(cookies)
        service, pool = make_service(FailureQrLogin, BrokenStore())
        try:
            with pytest.raises(XhsApiError) as caught:
                data = await service.create_qrcode('qr_one')
                await service.login_qrcode(data['session_id'])
            assert caught.value.status_code == (429 if phase == 'poll' else 502)
            assert pool.stats()['total'] == 0
            assert 'private-' not in str(caught.value)
            session = service.sessions[caught.value.details['session_id']]
            assert session.status == ('verification_required' if phase == 'poll' else 'failed')
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_qr_waiters_rejoin_and_do_not_block_sms():
    """请求断开后可重试等待，后台轮询与提交只执行一次。"""
    async def scenario():
        class WaitingQrLogin(FakeQrLogin):
            confirmed = False
            def check_qrcode_status(self, *args):
                if self.confirmed:
                    self.states = ('验证成功',)
                else:
                    self.states = ('请扫描二维码',)
                return super().check_qrcode_status(*args)
        store = make_store()
        service, pool = make_service(WaitingQrLogin, store)
        waiter = None
        try:
            data = await service.create_qrcode('qr_one')
            session = service.sessions[data['session_id']]
            login = session.login
            waiter = asyncio.create_task(service.login_qrcode(session.session_id))
            for _ in range(100):
                if login.polls > 1:
                    break
                await asyncio.sleep(0.001)
            assert login.polls > 1
            background = session.task
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not background.cancelled()
            sms = await service.send_sms(SmsSessionRequest(account_id='sms_one', phone='13800000000'))
            with pytest.raises(XhsApiError) as caught:
                await service.login_qrcode(sms['session_id'])
            assert caught.value.status_code == 409
            resumed = asyncio.create_task(service.login_qrcode(session.session_id))
            await asyncio.sleep(0)
            assert session.task is background
            login.confirmed = True
            assert (await resumed)['status'] == 'succeeded'
            records = store.load_latest()
            assert len(records) == 1
            for record in records:
                record.auth.close()
        finally:
            await service.shutdown()
            pool.close()
            store.close()
    asyncio.run(scenario())


def test_qr_session_expiry_and_shutdown_close_clients():
    async def scenario():
        service, pool = make_service()
        try:
            data = await service.create_qrcode('qr_one')
            session = service.sessions[data['session_id']]
            login = session.login
            session.expires_at = 0
            with pytest.raises(XhsApiError) as caught:
                await service.login_qrcode(session.session_id)
            assert caught.value.status_code == 410 and login.closed
            second = await service.create_qrcode('qr_one')
            active = service.sessions[second['session_id']].login
        finally:
            await service.shutdown()
        assert active.closed and pool.stats()['total'] == 0
    asyncio.run(scenario())

# coding=utf-8
import asyncio
import base64
import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import main
from app.account_pool import AccountPool
from app.account_store import CredentialRecord
from builder.auth import DouyinAuth
from dy_apis.login_api import (
    BrowserVerificationRequiredError,
    DYLoginApi,
    SmsVerificationInteractionError,
)
from app.qr_login_service import QrLoginService
from app.spider_service import SpiderService


@pytest.fixture(autouse=True)
def api_environment(monkeypatch):
    """扫码 API 测试不读取开发机真实环境文件。"""
    monkeypatch.setattr(main, 'load_environment', lambda: None)
    monkeypatch.setattr(main, 'get_app_env', lambda: 'dev')
    monkeypatch.delenv('QR_LOGIN_HEADLESS', raising=False)
    monkeypatch.delenv('QR_DEBUG_SCREENSHOT_ENABLED', raising=False)
    monkeypatch.delenv('LOG_DIR', raising=False)


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


class SmsVerificationLoginAPI:
    def __init__(self):
        self.submitted_code = None

    async def login_grab_ticket(self, **kwargs):
        await kwargs['qrcode_callback'](b'sms-png')
        await kwargs['verification_callback']('verification_required')
        command_queue = kwargs['verification_command_queue']
        while True:
            command = await command_queue.get()
            if command['action'] == 'request_sms':
                await kwargs['verification_callback']('waiting_sms_code')
            elif command['action'] == 'submit_sms':
                self.submitted_code = command['code']
                await kwargs['verification_callback']('verifying_sms')
                return make_auth('sms')


class RetrySmsVerificationLoginAPI:
    def __init__(self):
        self.actions = []

    async def login_grab_ticket(self, **kwargs):
        await kwargs['qrcode_callback'](b'retry-sms-png')
        callback = kwargs['verification_callback']
        commands = kwargs['verification_command_queue']
        await callback('verification_required')
        while True:
            command = await commands.get()
            self.actions.append(command['action'])
            if command['action'] == 'request_sms':
                await callback('waiting_sms_code')
            elif command['action'] == 'submit_sms':
                await callback('verifying_sms')
                await callback(
                    'waiting_sms_code',
                    'QR_SMS_VERIFICATION_STALLED',
                    '短信验证未完成，请确认验证码后重试',
                )
            elif command['action'] == 'resend_sms':
                await callback('waiting_sms_code')


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


def test_sms_verification_flow_continues_same_qr_session():
    login_api = SmsVerificationLoginAPI()
    client, store, pool = make_client(login_api)
    with client:
        created = client.post(
            '/api/v1/douyin/auth/qr-sessions',
            json={'account_id': 'sms-account'},
        )
        session_id = created.json()['data']['session_id']
        verification = wait_for_status(client, session_id, 'verification_required')
        # 测试服务原始会话仅 5 秒，短信阶段应获得独立延长期限。
        verification_data = verification.json()['data']
        created_at = datetime.fromisoformat(verification_data['created_at'].replace('Z', '+00:00'))
        expires_at = datetime.fromisoformat(verification_data['expires_at'].replace('Z', '+00:00'))
        assert (expires_at - created_at).total_seconds() > 100

        requested = client.post(
            f'/api/v1/douyin/auth/qr-sessions/{session_id}/sms/request'
        )
        assert requested.status_code == 200
        assert requested.json()['data']['status'] in {
            'requesting_sms', 'waiting_sms_code',
        }
        wait_for_status(client, session_id, 'waiting_sms_code')

        verified = client.post(
            f'/api/v1/douyin/auth/qr-sessions/{session_id}/sms/verify',
            json={'code': '123456'},
        )
        assert verified.status_code == 200
        assert '123456' not in verified.text
        wait_for_status(client, session_id, 'succeeded')

    assert login_api.submitted_code == '123456'
    assert store.calls[0][0] == 'sms-account'
    assert pool.stats()['available'] == 1


def test_sms_endpoints_reject_wrong_session_state_and_invalid_code():
    client, _, _ = make_client(SlowLoginAPI())
    with client:
        created = client.post(
            '/api/v1/douyin/auth/qr-sessions',
            json={'account_id': 'sms-account'},
        )
        session_id = created.json()['data']['session_id']

        requested = client.post(
            f'/api/v1/douyin/auth/qr-sessions/{session_id}/sms/request'
        )
        invalid_code = client.post(
            f'/api/v1/douyin/auth/qr-sessions/{session_id}/sms/verify',
            json={'code': '12ab'},
        )

    assert requested.status_code == 409
    assert requested.json()['error']['code'] == 'QR_SMS_NOT_AVAILABLE'
    assert invalid_code.status_code == 422
    assert invalid_code.json()['error']['code'] == 'INVALID_REQUEST'


def test_sms_verification_can_retry_and_resend_after_page_stalls():
    login_api = RetrySmsVerificationLoginAPI()
    client, _, _ = make_client(login_api)
    with client:
        created = client.post(
            '/api/v1/douyin/auth/qr-sessions',
            json={'account_id': 'retry-sms-account'},
        )
        session_id = created.json()['data']['session_id']
        wait_for_status(client, session_id, 'verification_required')

        client.post(f'/api/v1/douyin/auth/qr-sessions/{session_id}/sms/request')
        wait_for_status(client, session_id, 'waiting_sms_code')
        client.post(
            f'/api/v1/douyin/auth/qr-sessions/{session_id}/sms/verify',
            json={'code': '123456'},
        )
        stalled = wait_for_status(client, session_id, 'waiting_sms_code')
        assert stalled.json()['data']['error']['code'] == 'QR_SMS_VERIFICATION_STALLED'

        resent = client.post(
            f'/api/v1/douyin/auth/qr-sessions/{session_id}/sms/request'
        )
        assert resent.status_code == 200
        wait_for_status(client, session_id, 'waiting_sms_code')

    assert login_api.actions == ['request_sms', 'submit_sms', 'resend_sms']


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


def test_debug_screenshot_switch_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv('QR_DEBUG_SCREENSHOT_ENABLED', 'sometimes')

    with pytest.raises(RuntimeError, match='QR_DEBUG_SCREENSHOT_ENABLED'):
        QrLoginService(MemoryStore(), AccountPool(), login_api=SuccessfulLoginAPI())


def test_debug_screenshot_switch_passes_session_path(tmp_path, monkeypatch):
    class RecordingLoginAPI:
        def __init__(self):
            self.options = None

        async def login_grab_ticket(self, **kwargs):
            self.options = kwargs
            await kwargs['qrcode_callback'](b'debug-png')
            return make_auth('debug')

    monkeypatch.setenv('QR_DEBUG_SCREENSHOT_ENABLED', 'true')
    monkeypatch.setenv('LOG_DIR', str(tmp_path))
    login_api = RecordingLoginAPI()
    client, _, _ = make_client(login_api)

    with client:
        response = client.post('/api/v1/douyin/auth/qr-sessions', json={
            'account_id': 'debug-account',
        })
        session_id = response.json()['data']['session_id']
        wait_for_status(client, session_id, 'succeeded')

    assert login_api.options['debug_screenshot_path'] == str(
        tmp_path / 'qr-debug' / f'qr-login-{session_id}-latest.png'
    )


def test_visible_login_waits_for_manual_verification_then_clicks(monkeypatch):
    class FakePage:
        def __init__(self):
            self.titles = ['验证码中间页', '抖音']

        def is_closed(self):
            return False

        async def title(self):
            return self.titles.pop(0)

    clicked_texts = []

    async def click_login(page):
        clicked_texts.append('登录')
        return True

    monkeypatch.setattr(DYLoginApi, '_click_login_entry', click_login)
    page = FakePage()
    asyncio.run(DYLoginApi.wait_and_click_login(
        page,
        time.time() + 1,
        headless=False,
        poll_interval=0,
    ))

    assert clicked_texts == ['登录']


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


def test_headless_verification_page_writes_debug_screenshot(tmp_path):
    class FakePage:
        def is_closed(self):
            return False

        async def title(self):
            return '验证码中间页'

        async def screenshot(self, **options):
            with open(options['path'], 'wb') as screenshot_file:
                screenshot_file.write(b'verification-page')

    screenshot_path = tmp_path / 'verification.png'
    with pytest.raises(BrowserVerificationRequiredError):
        asyncio.run(DYLoginApi.wait_and_click_login(
            FakePage(),
            time.time() + 1,
            headless=True,
            poll_interval=0,
            debug_screenshot_path=str(screenshot_path),
        ))

    assert screenshot_path.read_bytes() == b'verification-page'


def test_debug_screenshot_overwrites_target_file(tmp_path):
    class FakePage:
        def __init__(self):
            self.options = None

        def is_closed(self):
            return False

        async def screenshot(self, **options):
            self.options = options
            with open(options['path'], 'wb') as screenshot_file:
                screenshot_file.write(b'png')

    page = FakePage()
    screenshot_path = tmp_path / 'debug' / 'latest.png'
    captured = asyncio.run(DYLoginApi.capture_debug_screenshot(
        page,
        str(screenshot_path),
    ))

    assert captured is True
    assert screenshot_path.read_bytes() == b'png'
    assert page.options == {'path': str(screenshot_path), 'full_page': True}


def test_qrcode_capture_uses_requested_render_timeout():
    class FakeElement:
        async def screenshot(self):
            return b'qr-png'

    class FakeHandle:
        def as_element(self):
            return FakeElement()

    class FakePage:
        def __init__(self):
            self.timeout = None

        async def wait_for_function(self, script, timeout):
            assert 'article img' in script
            self.timeout = timeout
            return FakeHandle()

    page = FakePage()
    image = asyncio.run(DYLoginApi.capture_login_qrcode(
        page,
        timeout_seconds=42.5,
    ))

    assert image == b'qr-png'
    assert page.timeout == 42500


def test_sms_page_detection_uses_visible_input_instead_of_placeholder_text():
    class FakeInput:
        async def is_visible(self):
            return True

        async def evaluate(self, script):
            return True

    class FakeLocator:
        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return FakeInput()

    class FakePage:
        def locator(self, selector):
            assert 'input' in selector
            return FakeLocator()

    # placeholder 不属于 innerText，但可见 input 仍应识别为短信验证页。
    detected = asyncio.run(DYLoginApi._is_identity_verification(
        FakePage(),
        '短信已发送至 135******56\n重新发送\n验证',
        verification_announced=True,
    ))

    assert detected is True


def test_sms_submit_waits_until_verification_button_is_enabled():
    class FakeButton:
        def __init__(self):
            self.enabled_checks = 0
            self.clicked = False

        async def is_visible(self):
            return True

        async def is_enabled(self):
            self.enabled_checks += 1
            return self.enabled_checks >= 2

        async def click(self):
            self.clicked = True

        async def evaluate(self, script):
            return True

    class FakeLocator:
        def __init__(self, button):
            self.button = button

        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return self.button

    class FakePage:
        def __init__(self):
            self.button = FakeButton()
            self.waits = []

        def get_by_role(self, role, name, exact):
            assert role == 'button'
            assert exact is True
            return FakeLocator(self.button)

        async def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

        async def evaluate(self, script, texts):
            return False

    page = FakePage()
    clicked = asyncio.run(DYLoginApi._click_enabled_button(
        page,
        ('验证',),
    ))

    assert clicked is True
    assert page.button.clicked is True
    assert page.waits == [100]


def test_login_click_skips_hidden_duplicate_text():
    class FakeButton:
        def __init__(self, visible):
            self.visible = visible
            self.clicked = False

        async def is_visible(self):
            return self.visible

        async def is_enabled(self):
            return True

        async def evaluate(self, script):
            return True

        async def click(self):
            self.clicked = True

    class FakeLocator:
        def __init__(self, buttons):
            self.buttons = buttons

        async def count(self):
            return len(self.buttons)

        def nth(self, index):
            return self.buttons[index]

    class FakePage:
        def __init__(self):
            self.hidden = FakeButton(False)
            self.visible = FakeButton(True)

        def get_by_role(self, role, name, exact):
            assert role == 'button'
            assert exact is True
            return FakeLocator([self.hidden, self.visible])

        async def evaluate(self, script, texts):
            raise AssertionError('可见按钮已点击，不应使用 DOM 回退')

    page = FakePage()
    clicked = asyncio.run(DYLoginApi._click_enabled_button(
        page,
        ('登录',),
    ))

    assert clicked is True
    assert page.hidden.clicked is False
    assert page.visible.clicked is True


def test_login_entry_prefers_stable_header_button(monkeypatch):
    class FakeButton:
        def __init__(self):
            self.clicked = False

        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        async def inner_text(self):
            return ' 登录 '

        async def get_attribute(self, name):
            assert name == 'aria-disabled'
            return 'false'

        async def evaluate(self, script):
            return True

        async def click(self, timeout):
            assert timeout == 3000
            self.clicked = True

    class FakeLocator:
        def __init__(self, button):
            self.button = button

        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return self.button

    class FakePage:
        def __init__(self):
            self.button = FakeButton()

        def locator(self, selector):
            assert selector == '#douyin-header-menuCt button[type="button"]'
            return FakeLocator(self.button)

    async def fallback_should_not_run(page, texts):
        raise AssertionError('稳定头部按钮已点击，不应执行通用回退')

    monkeypatch.setattr(
        DYLoginApi,
        '_click_enabled_button',
        fallback_should_not_run,
    )
    page = FakePage()
    clicked = asyncio.run(DYLoginApi._click_login_entry(page))

    assert clicked is True
    assert page.button.clicked is True


def test_sms_submit_can_click_non_semantic_div_button():
    class EmptyLocator:
        async def count(self):
            return 0

    class FakePage:
        def __init__(self):
            self.clicked_texts = None

        def get_by_role(self, role, name, exact):
            return EmptyLocator()

        async def evaluate(self, script, texts):
            self.clicked_texts = texts
            return True

        async def wait_for_timeout(self, milliseconds):
            raise AssertionError('div 已点击，不应继续等待')

    page = FakePage()
    clicked = asyncio.run(DYLoginApi._click_enabled_button(
        page,
        ('验证',),
    ))

    assert clicked is True
    assert page.clicked_texts == ['验证']


def test_sms_submit_types_sequentially_before_clicking_button(monkeypatch):
    class FakeInput:
        def __init__(self):
            self.actions = []

        async def is_visible(self):
            return True

        async def evaluate(self, script, value=None):
            if 'String(input.value' in script:
                return 6
            return True

        async def click(self):
            self.actions.append(('click', None))

        async def fill(self, value):
            self.actions.append(('fill', value))

        async def press_sequentially(self, value, delay):
            self.actions.append(('type', len(value), delay))

    class FakeButton(FakeInput):
        async def is_enabled(self):
            return True

    class FakeLocator:
        def __init__(self, element):
            self.element = element

        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return self.element

    class FakePage:
        def __init__(self):
            self.input = FakeInput()
            self.button = FakeButton()

        def locator(self, selector):
            return FakeLocator(self.input)

        def get_by_role(self, role, name, exact):
            return FakeLocator(self.button)

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 150

        async def evaluate(self, script, texts):
            raise AssertionError('语义按钮已点击，不应使用 DOM 回退')

    page = FakePage()
    async def no_known_submit(*args, **kwargs):
        return False

    monkeypatch.setattr(DYLoginApi, '_click_known_sms_submit', no_known_submit)
    monkeypatch.setattr(DYLoginApi, '_click_second_verify_submit', no_known_submit)
    asyncio.run(DYLoginApi.submit_sms_verification(page, '123456'))

    assert page.input.actions == [
        ('click', None),
        ('fill', ''),
        ('type', 6, 80),
    ]
    assert page.button.actions == [('click', None)]


def test_sms_submit_reports_safe_stage_when_button_is_not_ready(monkeypatch):
    class FakeInput:
        async def is_visible(self):
            return True

        async def evaluate(self, script, value=None):
            if 'String(input.value' in script:
                return 6
            return True

        async def click(self):
            pass

        async def fill(self, value):
            pass

        async def press_sequentially(self, value, delay):
            pass

    class FakeLocator:
        async def count(self):
            return 1

        def nth(self, index):
            return FakeInput()

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 150

    async def button_not_ready(*args, **kwargs):
        return False

    monkeypatch.setattr(DYLoginApi, '_click_known_sms_submit', button_not_ready)
    monkeypatch.setattr(DYLoginApi, '_click_second_verify_submit', button_not_ready)
    monkeypatch.setattr(DYLoginApi, '_click_enabled_button', button_not_ready)

    with pytest.raises(SmsVerificationInteractionError) as captured:
        asyncio.run(DYLoginApi.submit_sms_verification(FakePage(), '123456'))

    assert captured.value.code == 'QR_SMS_BUTTON_NOT_READY'
    assert '123456' not in captured.value.safe_message


def test_sms_input_falls_back_to_native_dom_setter(monkeypatch):
    class FakeInput:
        def __init__(self):
            self.dom_value_length = None

        async def is_visible(self):
            return True

        async def click(self):
            pass

        async def fill(self, value):
            pass

        async def press_sequentially(self, value, delay):
            raise RuntimeError('keyboard unavailable')

        async def type(self, value, delay):
            raise RuntimeError('legacy keyboard unavailable')

        async def evaluate(self, script, value=None):
            if 'String(input.value' in script:
                return self.dom_value_length
            if value is None:
                return True
            self.dom_value_length = len(value)

    class FakeLocator:
        def __init__(self, element):
            self.element = element

        async def count(self):
            return 1

        def nth(self, index):
            return self.element

    class FakePage:
        def __init__(self):
            self.input = FakeInput()

        def locator(self, selector):
            return FakeLocator(self.input)

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 150

    async def button_clicked(*args, **kwargs):
        return True

    monkeypatch.setattr(DYLoginApi, '_click_known_sms_submit', button_clicked)
    monkeypatch.setattr(DYLoginApi, '_click_second_verify_submit', button_clicked)
    page = FakePage()
    asyncio.run(DYLoginApi.submit_sms_verification(page, '123456'))

    assert page.input.dom_value_length == 6


def test_sms_submit_prefers_stable_douyin_component_ids():
    class FakeInput:
        async def is_visible(self):
            return True

        async def evaluate(self, script):
            return True

    class FakeSubmit:
        def __init__(self):
            self.clicked = False

        async def is_visible(self):
            return True

        async def evaluate(self, script):
            return True

        async def inner_text(self):
            return '验证'

        async def click(self, timeout):
            assert timeout == 2000
            self.clicked = True

    class FakeLocator:
        def __init__(self, element):
            self.element = element

        async def count(self):
            return 1

        def nth(self, index):
            return self.element

    class FakePage:
        def __init__(self):
            self.selectors = []
            self.input = FakeInput()
            self.submit = FakeSubmit()

        def locator(self, selector):
            self.selectors.append(selector)
            element = self.submit if selector == '#douyin_login_comp_btn_id' else self.input
            return FakeLocator(element)

    page = FakePage()
    input_locator = asyncio.run(DYLoginApi._find_visible_sms_input(page))
    submitted = asyncio.run(DYLoginApi._click_known_sms_submit(page))

    assert input_locator is page.input
    assert page.selectors[0] == '#uc-second-verify input#button-input'
    assert submitted is True
    assert page.submit.clicked is True


def test_second_verify_submit_waits_for_disabled_class_to_clear():
    class FakeSubmit:
        def __init__(self):
            self.enabled_checks = 0
            self.clicked = False

        async def is_visible(self):
            return True

        async def evaluate(self, script):
            if "className.includes('disabled')" in script:
                self.enabled_checks += 1
                return self.enabled_checks >= 2
            if 'element.click()' in script:
                assert 'dataset.codexSmsSubmit' in script
                self.clicked = True
            return True

        async def inner_text(self):
            return '验证'

        async def click(self, timeout):
            raise AssertionError('二次验证按钮不应使用 Playwright click')

    class FakeLocator:
        def __init__(self, element):
            self.element = element

        def get_by_text(self, text, exact):
            assert text == '验证'
            assert exact is True
            return self

        async def count(self):
            return 1

        def nth(self, index):
            return self.element

    class FakePage:
        def __init__(self):
            self.submit = FakeSubmit()
            self.waits = []
            self.probe = {'clicked': True, 'trusted': False}

        def locator(self, selector):
            assert selector == '#uc-second-verify'
            return FakeLocator(self.submit)

        async def evaluate(self, script):
            if '__douyinSmsVerifyClick = {' in script:
                assert 'document.addEventListener' in script
                assert 'dataset.codexSmsSubmit' in script
                return True
            if '__douyinSmsVerifyClick || null' in script:
                return self.probe
            raise AssertionError('未预期的页面脚本')

        async def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = FakePage()
    submitted = asyncio.run(DYLoginApi._click_second_verify_submit(page))

    assert submitted is True
    assert page.submit.clicked is True
    assert page.waits == [100, 50]


def test_second_verify_submit_rejects_silent_click_failure():
    class FakeSubmit:
        async def is_visible(self):
            return True

        async def evaluate(self, script):
            return True

        async def inner_text(self):
            return '验证'

    class FakeLocator:
        def get_by_text(self, text, exact):
            assert text == '验证'
            assert exact is True
            return self

        async def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return FakeSubmit()

    class FakePage:
        def locator(self, selector):
            assert selector == '#uc-second-verify'
            return FakeLocator()

        async def evaluate(self, script):
            if '__douyinSmsVerifyClick = {' in script:
                return True
            if '__douyinSmsVerifyClick || null' in script:
                return {'clicked': False, 'trusted': False}
            raise AssertionError('未预期的页面脚本')

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 50

    with pytest.raises(SmsVerificationInteractionError) as captured:
        asyncio.run(DYLoginApi._click_second_verify_submit(FakePage()))

    assert captured.value.code == 'QR_SMS_BUTTON_CLICK_NOT_RECEIVED'


def test_known_sms_submit_uses_dom_click_when_overlay_blocks_mouse():
    class FakeSubmit:
        def __init__(self):
            self.evaluate_calls = 0

        async def is_visible(self):
            return True

        async def evaluate(self, script):
            self.evaluate_calls += 1
            return True

        async def inner_text(self):
            return '验证'

        async def click(self, timeout):
            raise RuntimeError('overlay intercepted click')

    class FakeLocator:
        def __init__(self, element):
            self.element = element

        async def count(self):
            return 1

        def nth(self, index):
            return self.element

    class FakePage:
        def __init__(self):
            self.submit = FakeSubmit()

        def locator(self, selector):
            assert selector == '#douyin_login_comp_btn_id'
            return FakeLocator(self.submit)

    page = FakePage()
    submitted = asyncio.run(DYLoginApi._click_known_sms_submit(page))

    assert submitted is True
    # 依次检查顶层命中、禁用状态，并触发原生 DOM click。
    assert page.submit.evaluate_calls == 3


def test_sms_controls_behind_identity_overlay_are_ignored():
    class ObscuredElement:
        async def is_visible(self):
            return True

        async def evaluate(self, script):
            return False

    class FakeLocator:
        async def count(self):
            return 1

        def nth(self, index):
            return ObscuredElement()

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    input_locator = asyncio.run(DYLoginApi._find_visible_sms_input(FakePage()))

    assert input_locator is None


def test_invalid_account_id_is_rejected():
    client, _, _ = make_client(SuccessfulLoginAPI())
    with client:
        response = client.post('/api/v1/douyin/auth/qr-sessions', json={'account_id': '中文账号'})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_REQUEST'

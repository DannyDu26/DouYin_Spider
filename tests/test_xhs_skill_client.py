"""验证 skill 客户端与真实应用路由的契约，不访问上游或发送真实短信。"""
import importlib.util
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
import warnings

from fastapi.testclient import TestClient
from PIL import Image
import pytest

import main
from app.account_pool import AccountPool
from app.xhs_service import XhsService
from test_xhs_api import FakeClient
from test_xhs_credentials import make_store
from test_xhs_qr_login import make_service


SCRIPT = Path(__file__).resolve().parents[1] / 'skills/fetch-xhs-data/scripts/xhs_client.py'
spec = importlib.util.spec_from_file_location('xhs_skill_client', SCRIPT)
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


@pytest.fixture
def api(monkeypatch):
    """将 urllib 传输接入测试应用，保留真实路由和入池逻辑。"""
    monkeypatch.setattr(main, 'load_environment', lambda: None)
    monkeypatch.setattr(main, 'get_app_env', lambda: 'dev')
    store = make_store()
    service, pool = make_service(store=store)
    douyin = SimpleNamespace(test_account_pinning_enabled=False, max_concurrent=1, max_concurrent_per_account=1,
                             account_pool=AccountPool(), account_stats=lambda: {'total': 0, 'available': 0})
    FakeClient.failure = None
    calls = []
    application = main.create_app(service=douyin, xhs_service=XhsService(pool, store, client_factory=FakeClient),
                                  xhs_login_service=service)
    try:
        with TestClient(application) as transport:
            def urlopen(request, *, timeout):
                path = urlparse(request.full_url).path
                payload = json.loads(request.data) if request.data else None
                calls.append((request.get_method(), path, timeout))
                response = transport.request(request.get_method(), path, json=payload)
                if response.status_code >= 400:
                    raise HTTPError(request.full_url, response.status_code, 'test', {}, BytesIO(response.content))
                return BytesIO(response.content)
            monkeypatch.setattr(skill, 'urlopen', urlopen)
            yield SimpleNamespace(client=skill.XhsClient('http://testserver/api/'), calls=calls, pool=pool)
    finally:
        store.close()


def run(api, *arguments):
    """统一解析真实命令参数，避免手工构建 Namespace 漏字段。"""
    return skill._run(api.client, skill._build_parser().parse_args(arguments))


def test_qr_login_creates_image_and_persists_account(api, tmp_path):
    path = tmp_path / '二维码.png'
    response = run(api, 'login-start', '--account-id', 'qr_skill', '--output', str(path))
    data = response['data']
    assert Path(data['qrcode_path']).is_absolute()
    with Image.open(path) as image:
        assert image.format == 'PNG' and image.width > 100
    assert api.pool.stats()['total'] == 0
    assert 'qrcode_data_url' not in data
    response = run(api, 'login-wait', '--session-id', data['session_id'], '--wait-timeout', '210')
    assert response['data']['status'] == 'succeeded'
    assert api.calls[-1] == ('POST', '/api/v1/xhs/auth/qr/login', 210)
    # 重试原会话不会重复提交，账号列表仅输出安全元数据。
    assert run(api, 'login-wait', '--session-id', data['session_id'])['data']['status'] == 'succeeded'
    accounts = run(api, 'accounts')['data']['items']
    assert len(accounts) == 1 and accounts[0]['account_id'] == 'qr_skill'
    assert accounts[0]['status'] == 'available'
    assert not any(secret in json.dumps(accounts) for secret in ('web_session', 'formal-session', 'fixture-secret'))


def test_qr_output_collision_preserves_file_and_reports_session(api, tmp_path):
    path = tmp_path / 'exists.png'
    path.write_bytes(b'existing')
    with pytest.raises(skill.ClientError) as caught:
        run(api, 'login-start', '--account-id', 'collision', '--output', str(path))
    assert caught.value.details['code'] == 'OUTPUT_EXISTS'
    assert caught.value.details['session_id']
    assert caught.value.details['request_id']
    assert path.read_bytes() == b'existing'


def test_sms_invalid_code_then_success(api, monkeypatch):
    answers = iter(['13800000000', '000000', '123456'])
    monkeypatch.setattr(skill.getpass, 'getpass', lambda prompt: next(answers))
    created = run(api, 'login-sms-send', '--account-id', 'sms_skill')
    assert created['data']['status'] == 'waiting_sms_code'
    assert '13800000000' not in json.dumps(created)
    session_id = created['data']['session_id']
    with pytest.raises(skill.ClientError) as caught:
        run(api, 'login-sms-verify', '--session-id', session_id)
    assert caught.value.details['code'] == 'XHS_SMS_INVALID_CODE'
    assert caught.value.details['http_status'] == 422
    assert caught.value.details['request_id']
    assert api.pool.stats()['total'] == 0
    assert run(api, 'login-sms-verify', '--session-id', session_id)['data']['status'] == 'succeeded'
    assert run(api, 'accounts')['data']['items'][0]['account_id'] == 'sms_skill'


def test_missing_session_returns_safe_error(api):
    with pytest.raises(skill.ClientError) as caught:
        run(api, 'login-wait', '--session-id', 'missing')
    assert caught.value.details['code'] == 'XHS_LOGIN_SESSION_NOT_FOUND'
    assert caught.value.details['session_id'] == 'missing'


def test_wait_timeout_can_rejoin_same_session(api, monkeypatch, tmp_path):
    data = run(api, 'login-start', '--account-id', 'timeout', '--output', str(tmp_path / 'qr.png'))['data']
    real_urlopen = skill.urlopen
    def timeout(request, **kwargs):
        raise URLError(TimeoutError('private-network-detail'))
    monkeypatch.setattr(skill, 'urlopen', timeout)
    with pytest.raises(skill.ClientError) as caught:
        run(api, 'login-wait', '--session-id', data['session_id'])
    assert caught.value.details['code'] == 'REQUEST_TIMEOUT'
    assert caught.value.details['session_id'] == data['session_id']
    assert 'private-network-detail' not in str(caught.value)
    monkeypatch.setattr(skill, 'urlopen', real_urlopen)
    assert run(api, 'login-wait', '--session-id', data['session_id'])['data']['status'] == 'succeeded'


def test_noninteractive_sms_does_not_send(api, monkeypatch):
    def unavailable(prompt):
        warnings.warn('test-no-hidden-input', skill.getpass.GetPassWarning)
        pytest.fail('不能退化为明文输入')
    monkeypatch.setattr(skill.getpass, 'getpass', unavailable)
    with pytest.raises(skill.ClientError) as caught:
        run(api, 'login-sms-send', '--account-id', 'no_tty')
    assert caught.value.details['code'] == 'INTERACTIVE_INPUT_REQUIRED'
    assert api.calls == []


@pytest.mark.parametrize('raw', [b'<html>private-proxy-page</html>', b'[]', b'null'])
def test_invalid_response_never_echoes_body(monkeypatch, raw):
    monkeypatch.setattr(skill, 'urlopen', lambda request, **kwargs: BytesIO(raw))
    with pytest.raises(skill.ClientError) as caught:
        skill.XhsClient('http://testserver/api/').request('GET', 'health')
    assert caught.value.details['code'] == 'INVALID_RESPONSE'
    assert 'private-proxy-page' not in str(caught.value)


def test_http_gateway_error_never_echoes_body(monkeypatch):
    def fail(request, **kwargs):
        raise HTTPError(request.full_url, 504, 'private-error', {}, BytesIO(b'private-proxy-page'))
    monkeypatch.setattr(skill, 'urlopen', fail)
    with pytest.raises(skill.ClientError) as caught:
        skill.XhsClient('http://testserver/api/').request('GET', 'health')
    assert caught.value.details == {'code': 'HTTP_ERROR', 'http_status': 504}
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('data_url', ['not-an-image', 'data:image/png;base64,AAAA', 'data:image/png;base64,====='])
def test_bad_qr_does_not_write_file(tmp_path, data_url):
    path = tmp_path / 'bad.png'
    with pytest.raises(skill.ClientError):
        skill._save_qrcode(data_url, str(path), 'session', False)
    assert not path.exists()


@pytest.mark.parametrize('command', [
    ['--timeout', 'nan', 'health'], ['--timeout', '0', 'health'],
    ['login-wait', '--session-id', 'session', '--wait-timeout', 'inf'],
    ['--base-url', 'https://user:secret@example.com/api/', 'health'],
])
def test_parser_rejects_invalid_configuration(command):
    with pytest.raises(SystemExit) as caught:
        skill._build_parser().parse_args(command)
    assert caught.value.code == 2


def test_environment_base_url_and_health(monkeypatch, api):
    monkeypatch.setenv('XHS_API_BASE_URL', 'http://testserver/api')
    args = skill._build_parser().parse_args(['health'])
    assert args.base_url == 'http://testserver/api/'
    assert run(api, 'health')['success'] is True
    assert api.calls[-1][1] == '/api/health'


def test_wait_does_not_accept_unfinished_success_response(monkeypatch):
    raw = json.dumps({'success': True, 'data': {'session_id': 'unfinished', 'status': 'waiting_scan'}}).encode()
    monkeypatch.setattr(skill, 'urlopen', lambda request, **kwargs: BytesIO(raw))
    api = SimpleNamespace(client=skill.XhsClient('http://testserver/api/'))
    with pytest.raises(skill.ClientError) as caught:
        run(api, 'login-wait', '--session-id', 'unfinished')
    assert caught.value.details['code'] == 'INVALID_RESPONSE'

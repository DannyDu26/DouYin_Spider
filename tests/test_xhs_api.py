"""离线上游测试，验证 API 分页、签名和错误分类。"""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from app.account_pool import AccountPool
from app.account_store import CredentialRecord
from app.xhs_schemas import NoteRequest, SearchNotesRequest, UserNotesRequest
from app.xhs_service import XhsService
from xhs_apis.errors import XhsApiError, XhsAuthenticationError, XhsRiskControlError
from xhs_apis.xhs_pc_apis import XHS_Apis
from xhs_utils.xhs_pc import XHSPcAuth


NOTE_ID = 'a' * 24
USER_ID = 'b' * 24
COMMENT_ID = 'c' * 24


def make_auth(label='one'):
    auth = XHSPcAuth.from_saved_state(
        f'a1={label.ljust(52, "0")}; web_session=fixture-session; gid=fixture-gid; loadts=1720000000000; webBuild=6.47.2',
        user_id=USER_ID, dsl='1720000000000',
    )
    auth.login_verified = True
    # 固定 DSL 输入，同时使用当前缓存时间，避免单测触发网络刷新。
    from xhs_utils.xhs_pc.state import now_ms
    auth.profile.session.dsllt = now_ms()
    return auth


class FakeClient:
    calls = []
    failure = None

    def __init__(self, auth):
        self.auth = auth

    def bootstrap(self):
        self.auth.login_verified = True

    def get_note_info(self, url):
        self.calls.append(('note', url))
        if self.failure:
            raise self.failure
        return {'items': [{'id': NOTE_ID, 'note_card': {'title': '原始笔记', 'extra': {'untouched': True}}}]}

    def get_user_info(self, user_id):
        self.calls.append(('user', user_id))
        return {'basic_info': {'nickname': '测试'}, 'raw_field': 1}

    def get_user_note_info(self, *args):
        self.calls.append(('notes', args))
        return {'notes': [{'note_id': NOTE_ID, 'xsec_token': 'next-token'}], 'cursor': 'next-page', 'has_more': True}

    def get_note_out_comment(self, *args):
        self.calls.append(('comments', args))
        return {'comments': [{'id': COMMENT_ID, 'sub_comment_cursor': 'reply-start'}], 'cursor': '', 'has_more': False}

    def get_note_inner_comment(self, *args):
        self.calls.append(('replies', args))
        return {'comments': [], 'cursor': 'end', 'has_more': False}

    def search_note(self, **kwargs):
        self.calls.append(('search', kwargs))
        return {'items': [{'id': NOTE_ID, 'xsec_token': 'token'}], 'has_more': True}


@pytest.fixture
def client(monkeypatch):
    # 隔离本机 .env，避免影响后续抖音默认配置测试。
    monkeypatch.setattr(main, 'load_environment', lambda: None)
    monkeypatch.setattr(main, 'get_app_env', lambda: 'dev')
    FakeClient.calls = []
    FakeClient.failure = None
    pool = AccountPool([CredentialRecord(1, 'xhs_one', None, make_auth())],
                       cooldown_failure_limit=0, manage_auth_resources=True)
    service = XhsService(pool, client_factory=FakeClient)
    douyin = SimpleNamespace(test_account_pinning_enabled=False, max_concurrent=1, account_pool=AccountPool(),
                            account_stats=lambda: {'total': 0, 'available': 0})
    with TestClient(main.create_app(service=douyin, xhs_service=service)) as instance:
        yield instance, service


@pytest.mark.parametrize('route,payload,key', [
    ('note_info', {'note_id': NOTE_ID, 'xsec_token': 'token'}, 'note'),
    ('user_info', {'user_id': USER_ID}, 'user'),
    ('user_notes', {'user_id': USER_ID, 'cursor': 'start'}, 'items'),
    ('note_comments', {'note_id': NOTE_ID, 'xsec_token': 'token'}, 'items'),
    ('note_sub_comments', {'note_id': NOTE_ID, 'root_comment_id': COMMENT_ID, 'xsec_token': 'token'}, 'items'),
    ('search_notes', {'keyword': '测试'}, 'items'),
])
def test_six_routes_keep_raw_items(client, route, payload, key):
    response = client[0].post('/api/v1/xhs/' + route, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['success'] and body['request_id'] == response.headers['x-request-id']
    assert body['data']['account_id'] == 'xhs_one'
    assert key in body['data']
    if route == 'note_info':
        assert body['data']['note']['note_card']['extra'] == {'untouched': True}
    if route == 'user_notes':
        assert body['data']['next_cursor'] == 'next-page'
        assert body['data']['items'][0]['xsec_token'] == 'next-token'


def test_search_context_continues(client):
    first = client[0].post('/api/v1/xhs/search_notes', json={'keyword': '猫'}).json()['data']
    second = client[0].post('/api/v1/xhs/search_notes', json={
        'keyword': '猫', 'page': first['next_page'],
        'search_id': first['search_id'], 'search_session_id': first['search_session_id'],
    })
    assert second.status_code == 200
    sent = FakeClient.calls[-1][1]
    assert sent['search_id'] == first['search_id']
    assert sent['search_session_id'] == first['search_session_id']


@pytest.mark.parametrize('payload', [
    {'note_id': NOTE_ID},
    {'note_id': 'invalid', 'xsec_token': 'token'},
    {'url': f'https://evil.example/explore/{NOTE_ID}?xsec_token=t'},
    {'url': f'https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=a', 'xsec_token': 'b'},
    {'url': f'https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=a&xsec_token=b'},
])
def test_invalid_note_parameters_never_call_upstream(client, payload):
    assert client[0].post('/api/v1/xhs/note_info', json=payload).status_code == 422
    assert not FakeClient.calls


def test_url_tokens_and_user_cursor(client):
    response = client[0].post('/api/v1/xhs/user_notes', json={
        'user_url': f'https://www.xiaohongshu.com/user/profile/{USER_ID}?xsec_token=a%2Bb&xsec_source=pc_user',
        'cursor': 'opaque/+cursor',
    })
    assert response.status_code == 200
    assert FakeClient.calls[-1] == ('notes', (USER_ID, 'opaque/+cursor', 'a+b', 'pc_user'))


def test_risk_control_cools_only_selected_account(client):
    FakeClient.failure = XhsRiskControlError('http_461')
    response = client[0].post('/api/v1/xhs/note_info', json={'note_id': NOTE_ID, 'xsec_token': 't'})
    assert response.status_code == 429
    assert client[1].account_pool.stats()['cooling'] == 1


def test_business_error_does_not_cool_account(client):
    FakeClient.failure = XhsApiError('XHS_NOTE_UNAVAILABLE', '笔记不可用')
    assert client[0].post('/api/v1/xhs/note_info', json={'note_id': NOTE_ID, 'xsec_token': 't'}).status_code == 502
    assert client[1].account_pool.stats()['available'] == 1


def test_auth_failover_uses_other_account():
    class Client(FakeClient):
        def get_user_info(self, user_id):
            if self.auth.a1.startswith('one'):
                raise XhsAuthenticationError()
            return {'raw': 'second'}
    pool = AccountPool([CredentialRecord(1, 'one', None, make_auth('one')),
                        CredentialRecord(2, 'two', None, make_auth('two'))],
                       cooldown_failure_limit=0, manage_auth_resources=True)
    service = XhsService(pool, client_factory=Client)
    from app.xhs_schemas import UserRequest
    result = service.user_info(UserRequest(user_id=USER_ID), 'request')
    assert result['account_id'] == 'two' and result['failover_count'] == 1
    service.close()


def test_signed_transport_keeps_body_and_cookie_domains():
    class Transport:
        calls = []
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return SimpleNamespace(status_code=200, url=url, cookies={'acw_tc': 'search-edge'},
                                   json=lambda: {'success': True, 'data': {'items': [], 'has_more': False}})
        def close(self):
            pass
    auth = make_auth()
    auth.http_client.close()
    auth.http_client = Transport()
    auth._cookie_store.update('edith.xiaohongshu.com', {'acw_tc': 'api-edge'})
    client = XHS_Apis(auth)
    client.search_note('中文关键词', search_id='search', search_session_id='session')
    method, url, kwargs = auth.http_client.calls[0]
    assert url.startswith('https://so.xiaohongshu.com/')
    assert 'acw_tc' not in kwargs['cookies']
    assert json.loads(kwargs['data'])['keyword'] == '中文关键词'
    assert kwargs['headers']['x-s'] and kwargs['headers']['x-s-common']
    assert kwargs['headers']['x-rap-param'].startswith('ByQ')
    assert auth.cookies_for_url('https://edith.xiaohongshu.com')['acw_tc'] == 'api-edge'
    assert auth.cookies_for_url('https://so.xiaohongshu.com')['acw_tc'] == 'search-edge'
    auth.close()


def test_search_page_two_requires_context():
    with pytest.raises(ValueError):
        SearchNotesRequest(keyword='猫', page=2)


def test_runtime_refresh_keeps_formal_session(monkeypatch):
    auth = make_auth()
    auth.update_cookies({'websectiga': 'old', 'web_session': 'formal-session'})
    auth.profile.session.last_tiga_update_time = 0
    class SecurityLogin:
        def __init__(self, **kwargs):
            self.dsl = kwargs['dsl']
        def _initialize_security(self, cookies):
            cookies.update(websectiga='new', web_session='unexpected-visitor')
            self.profile.mark_tiga_updated()
            self.dsl = 'new-anchor'
    monkeypatch.setattr('app.xhs_service.XHSLoginApi', SecurityLogin)
    XhsService.prepare_runtime(auth)
    assert auth.profile.cookie_map['web_session'] == 'formal-session'
    assert auth.profile.cookie_map['websectiga'] == 'new'
    assert auth.dsl == 'new-anchor' and not auth.login_verified
    auth.close()

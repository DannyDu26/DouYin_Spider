"""用户作品链路的签名、会话和分页回归测试。"""
import json
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
from curl_cffi.requests.exceptions import RequestException

from dy_apis.douyin_api import DouyinAPI
from utils.user_read.ab_pure import ABogusPureSigner
from utils.user_read.client import UserReadClient, get_user_read_client
from utils.user_read.web_sign import sign_url


def test_signatures_match_reference_project_vectors():
    # 固定向量由成功项目独立计算，防止移植时改变签名算法。
    query = 'aid=6383&sec_user_id=sec-user&whale_cut_token=&msToken=test-token'
    assert ABogusPureSigner(fixed=True).sign_query(query, host='www.douyin.com') == (
        'df0bgq6idxW5cdMSuOTES1nlrHnMNsWyOzJ/WSol9PLlbwUGXbYeYYOWaxqEbMdf'
        'pWpwiFV7ZdGMYnncF07TZCHkLmpDSmwWkUA5V66oZ1wXbMiQLNfBCw8LeJtbWOvE'
        'mAojJ1UlWtmO2dC4LpaTUBlytApismipQHabdc4aE9ef6zT9Bqq2uxSdO7zq0E=='
    )
    url = ('https://www.douyin.com/aweme/v1/web/aweme/post/'
           '?name=%E4%B8%AD%E6%96%87&a=hello+world&whale_cut_token='
           '&a_bogus=A%2BB%3D&verifyFp=fp')
    assert sign_url(url, ts=1720000000, uifid='test-uifid') == (
        'https://www.douyin.com/aweme/v1/web/aweme/post/'
        '?name=%E4%B8%AD%E6%96%87&a=hello%20world&whale_cut_token='
        '&a_bogus=A%2BB%3D&verifyFp=fp&uifid=test-uifid&timestamp=1720000000'
        '&x-secsdk-web-signature=1d6fa9db8186ec94a7f180534edf09c2'
    )


class FakeCookies(dict):
    def set(self, name, value, **kwargs):
        self[name] = value

    def delete(self, name, **kwargs):
        self.pop(name, None)

    def get_dict(self):
        return dict(self)


class FakeSession:
    def __init__(self):
        self.cookies = FakeCookies()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        path = urlsplit(url).path
        data = {'id': 'real-device-id'} if path.endswith('/query/user') else {'status_code': 0}
        return SimpleNamespace(
            status_code=200, text=json.dumps(data), url=url, history=[],
            cookies=FakeCookies({'UIFID': 'updated-uifid'}),
        )

    def close(self):
        pass


@pytest.fixture
def fake_session(monkeypatch):
    from utils.user_read import client
    monkeypatch.setattr(client.requests, 'Session', FakeSession)


def make_auth():
    return SimpleNamespace(
        cookie={'sessionid': 'account-session', 'UIFID': 'initial-uifid', 's_v_web_id': 'fp'},
        msToken='stable-token',
    )


def test_profile_and_posts_share_session_device_id_and_cookie_updates(fake_session):
    auth = make_auth()
    client = get_user_read_client(auth)
    url = 'https://www.douyin.com/user/sec-user?from_tab_name=main&vid=123'
    client.get_user_info(url)
    client.get_user_work_info(url, '0')
    client.get_user_work_info(url, '1234')

    calls = client.session.calls
    assert len(calls) == 4  # 设备号只获取一次，后续分页复用。
    assert calls[0][0].endswith('/query/user')
    assert calls[1][1]['params']['webid'] == 'real-device-id'
    assert auth.cookie['UIFID'] == 'updated-uifid'
    assert get_user_read_client(auth) is client
    for request_url, kwargs in calls[2:]:
        params = dict(parse_qsl(urlsplit(request_url).query, keep_blank_values=True))
        assert params['webid'] == 'real-device-id'
        assert params['uifid'] == 'updated-uifid'
        assert kwargs['headers']['uifid'] == 'updated-uifid'
        assert params['sec_user_id'] == 'sec-user'
        assert params['need_time_list'] == ('1' if params['max_cursor'] == '0' else '0')
        assert kwargs['http_version'] == 'v2'
        assert kwargs['default_headers'] is False
        assert kwargs['verify'] is True
        assert kwargs['timeout'] == (10.0, 30.0)
        assert 'cookies' not in kwargs
        assert 'params' not in kwargs
        assert 'Chrome/' + params['browser_version'] in kwargs['headers']['user-agent']


def test_accounts_have_separate_sessions_and_device_caches(fake_session):
    first, second = get_user_read_client(make_auth()), get_user_read_client(make_auth())
    assert first.session is not second.session
    first._webid = 'first-device'
    assert second.webid == 'real-device-id'
    assert first.webid == 'first-device'


def test_webid_fallback_handles_transport_error(fake_session, monkeypatch):
    client = UserReadClient(make_auth())

    def fail(*args, **kwargs):
        raise RequestException('temporary failure')

    monkeypatch.setattr(client.session, 'get', fail)
    # 设备接口及主页暂时失败时仍按成功项目缓存一个后备设备号。
    assert client.webid.isdigit()
    assert client.webid == client.webid


def test_page_limit_and_cursor_are_preserved(monkeypatch):
    cursors = []

    def get_page(auth, url, cursor):
        cursors.append(cursor)
        return {'status_code': 0, 'aweme_list': [{'aweme_id': str(len(cursors))}],
                'max_cursor': len(cursors) * 100, 'has_more': 1}

    monkeypatch.setattr(DouyinAPI, 'get_user_work_info', get_page)
    result = DouyinAPI.get_user_some_work_info(None, 'https://www.douyin.com/user/abc', 2)
    assert cursors == ['0', '100']
    assert [item['aweme_id'] for item in result] == ['1', '2']


@pytest.mark.parametrize('payload', [
    {'status_code': 1, 'aweme_list': []},
    {'status_code': 0},
    {'status_code': 0, 'aweme_list': None},
])
def test_invalid_upstream_list_is_not_reported_as_empty_success(monkeypatch, payload):
    monkeypatch.setattr(DouyinAPI, 'get_user_work_info', lambda *args: payload)
    with pytest.raises(ValueError, match='aweme_list'):
        DouyinAPI.get_user_some_work_info(None, 'https://www.douyin.com/user/abc', 1)


def test_empty_last_page_is_valid(monkeypatch):
    monkeypatch.setattr(DouyinAPI, 'get_user_work_info', lambda *args: {
        'status_code': 0, 'aweme_list': [], 'has_more': 0,
    })
    assert DouyinAPI.get_user_some_work_info(None, 'https://www.douyin.com/user/abc', 1) == []


def test_homepage_trailing_slash_keeps_user_id(fake_session):
    client = UserReadClient(make_auth())
    client._webid = 'device-id'
    client.get_user_work_info('https://www.douyin.com/user/sec-user/?vid=123', '0')
    params = dict(parse_qsl(urlsplit(client.session.calls[0][0]).query))
    assert params['sec_user_id'] == 'sec-user'

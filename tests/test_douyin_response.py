# coding=utf-8
import json
from types import SimpleNamespace

import pytest

import dy_apis.douyin_api as douyin_module
from builder.params import Params
from dy_apis.douyin_api import DouyinAPI, DouyinAuthenticationError, parse_douyin_response


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, url='', history=None):
        self.status_code = status_code
        self.text = json.dumps(payload or {}) if text is None else text
        self.url = url
        self.history = history or []


def test_http_authentication_failure_is_detected():
    with pytest.raises(DouyinAuthenticationError):
        parse_douyin_response(FakeResponse(status_code=401))


@pytest.mark.parametrize('message', ['登录失效，请重新登录', '请先登录', '请登录后重试'])
def test_explicit_login_message_is_detected(message):
    response = FakeResponse(payload={'status_code': 1, 'status_msg': message})
    with pytest.raises(DouyinAuthenticationError):
        parse_douyin_response(response)


def test_passport_service_failure_is_not_misclassified_as_account_failure():
    payload = {'status_code': 500, 'status_msg': 'passport service unavailable'}

    assert parse_douyin_response(FakeResponse(payload=payload)) == payload


def test_normal_business_response_is_preserved():
    payload = {'status_code': 0, 'aweme_detail': {'aweme_id': '1'}}
    assert parse_douyin_response(FakeResponse(payload=payload)) == payload


def test_login_redirect_returning_html_is_authentication_failure():
    response = FakeResponse(
        text='<html>login</html>',
        url='https://sso.douyin.com/login/',
        history=[SimpleNamespace(
            url='https://www.douyin.com/aweme/v1/web/aweme/detail/',
            headers={'location': 'https://sso.douyin.com/login/'},
        )],
    )
    with pytest.raises(DouyinAuthenticationError):
        parse_douyin_response(response)


def test_ordinary_non_json_response_is_not_misclassified_as_authentication_failure():
    response = FakeResponse(
        text='<html>temporary upstream page</html>',
        url='https://www.douyin.com/error',
    )
    with pytest.raises(json.JSONDecodeError):
        parse_douyin_response(response)


def test_core_api_requests_use_timeout_and_verified_tls(monkeypatch, tmp_path):
    """核心 HTTP 抓取入口必须共享超时和 CA 校验。"""
    ca_bundle = tmp_path / 'company-ca.pem'
    ca_bundle.write_text('test ca', encoding='utf-8')
    monkeypatch.setenv('DOUYIN_CONNECT_TIMEOUT_SECONDS', '2')
    monkeypatch.setenv('DOUYIN_READ_TIMEOUT_SECONDS', '7')
    monkeypatch.setenv('DOUYIN_CA_BUNDLE', str(ca_bundle))
    monkeypatch.setattr(Params, 'with_web_id', lambda self, *args, **kwargs: self)
    monkeypatch.setattr(Params, 'with_a_bogus', lambda self, *args, **kwargs: self)
    monkeypatch.setattr(douyin_module, 'generate_a_bogus_pure', lambda *args: 'signed')

    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        return FakeResponse(payload={
            'status_code': 0,
            'data': [],
            'has_more': 0,
            'aweme_detail': {'aweme_id': '123'},
        })

    monkeypatch.setattr(douyin_module.requests, 'get', fake_get)
    auth = SimpleNamespace(cookie={'s_v_web_id': 'fp'}, msToken='token')

    DouyinAPI.get_user_work_info(auth, 'https://www.douyin.com/user/abc', '0')
    DouyinAPI.get_work_info(auth, 'https://www.douyin.com/video/123')
    DouyinAPI.get_work_out_comment(
        auth,
        'https://www.douyin.com/video/123',
        '7',
        '13',
    )
    DouyinAPI.get_work_out_comment(
        auth,
        'https://www.douyin.com/discover?modal_id=456&from=video',
        '0',
        '20',
    )
    DouyinAPI.get_work_inner_comment(
        auth,
        {'aweme_id': '123', 'cid': '789'},
        '4',
        '9',
    )
    DouyinAPI.get_user_info(auth, 'https://www.douyin.com/user/abc')
    DouyinAPI.search_general_work(auth, 'query')

    assert len(calls) == 7
    assert all(call['timeout'] == (2.0, 7.0) for call in calls)
    assert all(call['verify'] == str(ca_bundle.resolve()) for call in calls)
    assert calls[2]['params']['cursor'] == '7'
    assert calls[2]['params']['count'] == '13'
    assert calls[3]['params']['aweme_id'] == '456'
    assert calls[3]['headers']['referer'] == 'https://www.douyin.com/video/456'
    assert calls[4]['params']['item_id'] == '123'
    assert calls[4]['params']['comment_id'] == '789'
    assert calls[4]['params']['cursor'] == '4'
    assert calls[4]['params']['count'] == '9'


def test_work_detail_prefers_minimal_params(monkeypatch):
    """作品详情应优先使用已验证可用的轻量参数。"""
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        return FakeResponse(payload={
            'status_code': 0,
            'aweme_detail': {'aweme_id': '123'},
        })

    monkeypatch.setattr(douyin_module.requests, 'get', fake_get)
    auth = SimpleNamespace(cookie={'s_v_web_id': 'fp'}, msToken='token')

    result = DouyinAPI.get_work_info(auth, 'https://www.douyin.com/video/123')

    assert result['aweme_detail']['aweme_id'] == '123'
    assert len(calls) == 1
    assert calls[0]['params'] == {
        'device_platform': 'webapp',
        'aid': '6383',
        'channel': 'channel_pc_web',
        'aweme_id': '123',
    }


def test_work_detail_falls_back_to_signed_request(monkeypatch):
    """轻量响应无详情时应继续使用完整签名请求。"""
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return FakeResponse(text='')
        return FakeResponse(payload={
            'status_code': 0,
            'aweme_detail': {'aweme_id': '123'},
        })

    monkeypatch.setattr(Params, 'with_web_id', lambda self, *args, **kwargs: self)
    monkeypatch.setattr(Params, 'with_a_bogus', lambda self, *args, **kwargs: self)
    monkeypatch.setattr(douyin_module.requests, 'get', fake_get)
    auth = SimpleNamespace(cookie={'s_v_web_id': 'fp'}, msToken='token')

    result = DouyinAPI.get_work_info(auth, 'https://www.douyin.com/video/123')

    assert result['aweme_detail']['aweme_id'] == '123'
    assert len(calls) == 2
    assert 'a_bogus' not in calls[0]['params']
    assert calls[1]['params']['verifyFp'] == 'fp'


@pytest.mark.parametrize('status_code', [401, 403])
def test_work_detail_minimal_request_propagates_auth_failure(monkeypatch, status_code):
    """轻量请求的明确认证失败不能被签名兜底掩盖。"""
    monkeypatch.setattr(
        douyin_module.requests,
        'get',
        lambda *args, **kwargs: FakeResponse(status_code=status_code),
    )
    auth = SimpleNamespace(cookie={'s_v_web_id': 'fp'}, msToken='token')

    with pytest.raises(DouyinAuthenticationError):
        DouyinAPI.get_work_info(auth, 'https://www.douyin.com/video/123')


@pytest.mark.parametrize('status_code', [401, 403])
def test_comment_request_propagates_http_authentication_failure(monkeypatch, status_code):
    """评论请求的明确认证失败必须交给账号池处理。"""
    monkeypatch.setattr(Params, 'with_web_id', lambda self, *args, **kwargs: self)
    monkeypatch.setattr(Params, 'with_a_bogus', lambda self, *args, **kwargs: self)
    monkeypatch.setattr(
        douyin_module.requests,
        'get',
        lambda *args, **kwargs: FakeResponse(status_code=status_code),
    )
    auth = SimpleNamespace(cookie={'s_v_web_id': 'fp'}, msToken='token')

    with pytest.raises(DouyinAuthenticationError):
        DouyinAPI.get_work_out_comment(auth, 'https://www.douyin.com/video/123')


@pytest.mark.parametrize('status_code', [401, 403])
def test_reply_request_propagates_http_authentication_failure(monkeypatch, status_code):
    """二级评论请求的认证失败也必须交给账号池处理。"""
    monkeypatch.setattr(Params, 'with_web_id', lambda self, *args, **kwargs: self)
    monkeypatch.setattr(Params, 'with_a_bogus', lambda self, *args, **kwargs: self)
    monkeypatch.setattr(
        douyin_module.requests,
        'get',
        lambda *args, **kwargs: FakeResponse(status_code=status_code),
    )
    auth = SimpleNamespace(cookie={'s_v_web_id': 'fp'}, msToken='token')

    with pytest.raises(DouyinAuthenticationError):
        DouyinAPI.get_work_inner_comment(
            auth,
            {'aweme_id': '123', 'cid': '789'},
        )


def test_search_stops_after_empty_page_even_when_upstream_claims_more(monkeypatch):
    calls = []

    def empty_page(*args, **kwargs):
        calls.append(args)
        return {'status_code': 0, 'data': [], 'has_more': 1}

    monkeypatch.setattr(DouyinAPI, 'search_general_work', empty_page)

    assert DouyinAPI.search_some_general_work(object(), 'query', 20, '0', '0') == []
    assert len(calls) == 1

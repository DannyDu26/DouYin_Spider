# coding=utf-8
import logging
import threading
import time
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import main
from app.account_pool import AccountPool
from app.account_store import CredentialRecord, CredentialStoreError
from app.api_schemas import SearchWorksRequest
from builder.auth import DouyinAuth
from dy_apis.douyin_api import DouyinAuthenticationError, DouyinRiskControlError
from app.spider_service import SpiderService


def make_work(work_id='100', nickname='测试用户'):
    """构造满足标准化函数要求的最小作品数据。"""
    return {
        'aweme_id': work_id,
        'aweme_type': 0,
        'desc': f'作品 {work_id}',
        'create_time': 1_700_000_000,
        'author': {
            'sec_uid': 'sec-user',
            'unique_id': 'user-id',
            'nickname': nickname,
            'signature': '签名',
            'avatar_thumb': {'url_list': ['https://example.com/avatar.jpg']},
        },
        'statistics': {
            'admire_count': 0,
            'digg_count': 1,
            'comment_count': 2,
            'collect_count': 3,
            'share_count': 4,
        },
        'video': {
            'cover': {'url_list': ['https://example.com/cover.jpg']},
            'play_addr': {'url_list': ['https://example.com/video.mp4']},
        },
        'images': [],
        'text_extra': [],
    }


def make_comment(comment_id='comment-1'):
    """构造最小一级评论数据。"""
    return {
        'cid': comment_id,
        'text': '测试评论',
        'create_time': 1_700_000_001,
        'digg_count': 3,
        'reply_comment_total': 0,
        'user': {'uid': 'comment-user', 'nickname': '评论用户'},
    }


class FakeDouyinAPI:
    def __init__(self):
        self.search_args = None
        self.comment_args = None
        self.sub_comment_args = None
        self.user_work_args = None
        self.work_info_urls = []
        self.comment_response = {
            'comments': [make_comment()],
            'cursor': 20,
            'has_more': 1,
        }
        self.sub_comment_response = {
            'comments': [make_comment('9002')],
            'cursor': 10,
            'has_more': 1,
        }

    def get_work_info(self, auth, url):
        self.work_info_urls.append(url)
        work_id = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]
        if work_id == '999':
            raise RuntimeError('secret-token=should-not-leak')
        return {'aweme_detail': make_work(work_id)}

    def get_user_info(self, auth, user_url):
        return {'user': {'nickname': '主页昵称', 'follower_count': 88}}

    def get_work_out_comment(self, auth, url, cursor, count):
        self.comment_args = (auth, url, cursor, count)
        return self.comment_response

    def get_work_inner_comment(self, auth, comment, cursor, count):
        self.sub_comment_args = (auth, comment, cursor, count)
        return self.sub_comment_response

    def get_user_some_work_info(self, auth, user_url, page_num):
        self.user_work_args = (auth, user_url, page_num)
        return [make_work(str(index)) for index in range(page_num)]

    def search_some_general_work(self, *args):
        self.search_args = args
        return {
            'items': [{'aweme_info': make_work('search-1')}],
            'has_more': True,
            'raw_page_counts': [25],
        }


@pytest.fixture
def fake_api():
    return FakeDouyinAPI()


@pytest.fixture(autouse=True)
def api_environment(monkeypatch):
    """API 测试固定为 dev，避免读取开发机真实配置。"""
    monkeypatch.setattr(main, 'load_environment', lambda: None)
    monkeypatch.setattr(main, 'get_app_env', lambda: 'dev')


@pytest.fixture
def client(fake_api):
    service = SpiderService(auth=object(), max_concurrent=2, douyin_api=fake_api)
    with TestClient(main.create_app(service)) as test_client:
        yield test_client


def test_health(client):
    response = client.get('/api/health', headers={'X-Request-ID': 'health-id'})
    body = response.json()
    assert response.status_code == 200
    assert len(body['request_id']) == 32
    assert body['request_id'] != 'health-id'
    assert response.headers['X-Request-ID'] == body['request_id']
    assert body == {
        'success': True,
        'request_id': body['request_id'],
        'data': {
            'status': 'ok',
            'environment': 'dev',
            'database': 'not_configured',
            'accounts': {'total': 1, 'available': 1, 'cooling': 0, 'invalid': 0},
                'max_concurrent_requests': 2,
                'max_concurrent_requests_per_account': 2,
                'test_account_pinning_enabled': False,
        },
    }


def test_openapi_contains_chinese_endpoint_documentation(client):
    """接口文档应包含分组说明、中文标题和请求示例。"""
    schema = client.get('/api/openapi.json').json()
    video_info = schema['paths']['/api/v1/douyin/video_info']['post']
    sub_comments = schema['paths']['/api/v1/douyin/video_sub_comments']['post']
    works_request = schema['components']['schemas']['WorksRequest']
    sub_comments_request = schema['components']['schemas']['VideoSubCommentsRequest']

    assert schema['info']['description'].startswith('公司内部使用的抖音多账号数据抓取 API')
    assert {tag['name'] for tag in schema['tags']} == {'system', 'auth', 'videos'}
    assert video_info['summary'] == '批量获取作品详情'
    assert '429' in video_info['responses']
    assert video_info['responses']['502']['description'].startswith('抖音上游')
    assert works_request['properties']['urls']['description'].startswith('抖音作品链接列表')
    assert works_request['examples'][0]['urls'][0].startswith('https://www.douyin.com/video/')
    # 二级评论恢复到正式文档，并提供参数说明和请求示例。
    assert sub_comments['summary'] == '获取视频二级评论'
    assert sub_comments_request['properties']['comment_id']['description'].startswith('需要查询回复')
    assert sub_comments_request['examples'][0]['comment_id'].isdigit()


def test_documentation_and_health_use_api_prefix(client):
    """文档、Schema 和健康检查统一使用 /api 前缀。"""
    assert client.get('/api/docs').status_code == 200
    assert client.get('/api/redoc').status_code == 200
    assert client.get('/api/openapi.json').status_code == 200
    assert client.get('/api/health').status_code == 200
    assert client.get('/docs').status_code == 404
    assert client.get('/openapi.json').status_code == 404
    assert client.get('/health').status_code == 404


def test_health_reports_actual_per_account_concurrency(fake_api):
    auth = DouyinAuth()
    auth.perepare_auth('sessionid=test; s_v_web_id=fp-test', '', '')
    pool = AccountPool([
        CredentialRecord(1, 'account-a', datetime.now(timezone.utc), auth),
    ], max_concurrent_per_account=1)
    service = SpiderService(account_pool=pool, max_concurrent=10, douyin_api=fake_api)

    with TestClient(main.create_app(service)) as test_client:
        response = test_client.get('/api/health')

    assert response.status_code == 200
    assert response.json()['data']['max_concurrent_requests'] == 10
    assert response.json()['data']['max_concurrent_requests_per_account'] == 1


def test_search_account_pinning_is_disabled_by_default(client):
    response = client.post('/api/v1/douyin/search_videos', json={
        'query': '测试关键词',
        'target_account_id': 'default',
    })

    assert response.status_code == 403
    assert response.json()['error']['code'] == 'ACCOUNT_PINNING_DISABLED'


def test_search_default_example_does_not_pin_an_account():
    schema = SearchWorksRequest.model_json_schema()

    assert 'target_account_id' not in schema['examples'][0]
    assert 'offset' not in schema['properties']
    assert schema['examples'][0]['limit'] == 25
    assert SearchWorksRequest(query='测试').limit == 25


def test_search_account_pinning_is_forced_off_in_prod(fake_api, monkeypatch):
    monkeypatch.setattr(main, 'get_app_env', lambda: 'prod')
    monkeypatch.setattr(main, 'configure_logging', lambda _app_env: None)
    service = SpiderService(
        auth=object(),
        douyin_api=fake_api,
        test_account_pinning_enabled=True,
    )

    with TestClient(main.create_app(service)) as test_client:
        health = test_client.get('/api/health')
        response = test_client.post('/api/v1/douyin/search_videos', json={
            'query': '测试关键词',
            'target_account_id': 'default',
        })

    assert health.json()['data']['test_account_pinning_enabled'] is False
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'ACCOUNT_PINNING_DISABLED'


def test_search_can_pin_enabled_test_account(fake_api):
    first_auth = DouyinAuth()
    first_auth.label = 'first'
    second_auth = DouyinAuth()
    second_auth.label = 'second'
    pool = AccountPool([
        CredentialRecord(1, 'account-a', datetime.now(timezone.utc), first_auth),
        CredentialRecord(2, 'account-b', datetime.now(timezone.utc), second_auth),
    ])
    service = SpiderService(
        account_pool=pool,
        max_concurrent=2,
        douyin_api=fake_api,
        test_account_pinning_enabled=True,
    )

    with TestClient(main.create_app(service)) as test_client:
        response = test_client.post('/api/v1/douyin/search_videos', json={
            'query': '测试关键词',
            'target_account_id': 'account-b',
        })

    assert response.status_code == 200
    assert response.json()['data']['account_id'] == 'account-b'
    assert response.json()['data']['failover_count'] == 0
    assert fake_api.search_args[0].label == 'second'


def test_pinned_risk_only_cools_actual_account(fake_api, monkeypatch):
    first_auth = DouyinAuth()
    second_auth = DouyinAuth()
    pool = AccountPool([
        CredentialRecord(1, 'account-a', datetime.now(timezone.utc), first_auth),
        CredentialRecord(2, 'account-b', datetime.now(timezone.utc), second_auth),
    ])
    service = SpiderService(
        account_pool=pool,
        douyin_api=fake_api,
        test_account_pinning_enabled=True,
    )
    monkeypatch.setattr(
        fake_api,
        'search_some_general_work',
        lambda *args: (_ for _ in ()).throw(DouyinRiskControlError('http_429')),
    )

    with TestClient(main.create_app(service)) as test_client:
        response = test_client.post('/api/v1/douyin/search_videos', json={
            'query': '测试关键词',
            'target_account_id': 'account-b',
        })

    statuses = {item['account_id']: item['status'] for item in pool.list_accounts()}
    assert response.status_code == 429
    assert statuses == {'account-a': 'available', 'account-b': 'cooling'}


def test_batch_works_supports_partial_success_without_leaking_error(client):
    response = client.post('/api/v1/douyin/video_info', json={
        'urls': [
            'https://www.douyin.com/video/101',
            'https://www.douyin.com/video/999',
        ],
    })
    body = response.json()
    assert response.status_code == 200
    assert body['data']['success_count'] == 1
    assert body['data']['failed_count'] == 1
    assert body['data']['items'][0]['work_id'] == '101'
    assert body['data']['errors'][0]['error'] == 'RuntimeError'
    assert 'should-not-leak' not in response.text


def test_batch_works_all_failed_returns_502(client):
    response = client.post('/api/v1/douyin/video_info', json={
        'urls': ['https://www.douyin.com/video/999'],
    })
    assert response.status_code == 502
    assert response.json()['error']['code'] == 'UPSTREAM_ERROR'
    assert response.json()['error']['details'][0]['error'] == 'RuntimeError'


def test_work_comments_returns_paginated_data_and_passes_parameters(client, fake_api, capfd):
    secret = 'private-query-value'
    comment_marker = 'confidential-comment-body'
    fake_api.comment_response['comments'][0]['text'] = comment_marker
    response = client.post('/api/v1/douyin/video_comments', json={
        'url': f'https://www.douyin.com/video/101?access_token={secret}',
        'cursor': 5,
        'count': 12,
    })
    captured = capfd.readouterr()
    body = response.json()

    assert response.status_code == 200
    assert body['data']['items'][0]['cid'] == 'comment-1'
    assert body['data']['total'] == 1
    assert body['data']['cursor'] == 5
    assert body['data']['next_cursor'] == 20
    assert body['data']['has_more'] is True
    assert body['data']['work_url'] == 'https://www.douyin.com/video/101'
    assert body['data']['account_id'] == 'default'
    assert body['data']['failover_count'] == 0
    assert fake_api.comment_args[1:] == (
        f'https://www.douyin.com/video/101?access_token={secret}',
        '5',
        '12',
    )
    assert comment_marker in response.text
    assert secret not in response.text
    assert secret not in captured.out
    assert secret not in captured.err
    assert comment_marker not in captured.out
    assert comment_marker not in captured.err


def test_work_comments_empty_page_is_success(client, fake_api):
    fake_api.comment_response = {'comments': None, 'cursor': '0', 'has_more': 0}

    response = client.post('/api/v1/douyin/video_comments', json={
        'url': 'https://www.douyin.com/video/101',
    })

    assert response.status_code == 200
    assert response.json()['data']['items'] == []
    assert response.json()['data']['total'] == 0
    assert response.json()['data']['has_more'] is False
    assert fake_api.comment_args[2:] == ('0', '20')


def test_work_comments_accepts_video_id(client, fake_api):
    response = client.post('/api/v1/douyin/video_comments', json={
        'url': None,
        'video_id': '101',
        'cursor': 3,
    })

    assert response.status_code == 200
    assert response.json()['data']['work_url'] == 'https://www.douyin.com/video/101'
    assert fake_api.comment_args[1:] == (
        'https://www.douyin.com/video/101',
        '3',
        '20',
    )


def test_video_sub_comments_returns_paginated_data(client, fake_api):
    response = client.post('/api/v1/douyin/video_sub_comments', json={
        'video_id': '101',
        'comment_id': '9001',
        'cursor': 5,
        'count': 12,
    })
    body = response.json()

    assert response.status_code == 200
    assert body['data']['items'][0]['cid'] == '9002'
    assert body['data']['total'] == 1
    assert body['data']['video_id'] == '101'
    assert body['data']['comment_id'] == '9001'
    assert body['data']['cursor'] == 5
    assert body['data']['next_cursor'] == 10
    assert body['data']['has_more'] is True
    assert body['data']['account_id'] == 'default'
    assert fake_api.sub_comment_args[1:] == (
        {'aweme_id': '101', 'cid': '9001'},
        '5',
        '12',
    )


def test_video_sub_comments_empty_page_is_success(client, fake_api):
    fake_api.sub_comment_response = {'comments': None, 'cursor': '0', 'has_more': 0}

    response = client.post('/api/v1/douyin/video_sub_comments', json={
        'video_id': '101',
        'comment_id': '9001',
    })

    assert response.status_code == 200
    assert response.json()['data']['items'] == []
    assert response.json()['data']['has_more'] is False
    assert fake_api.sub_comment_args[2:] == ('0', '20')


@pytest.mark.parametrize('payload', [
    {},
    {'video_id': 'not-a-number', 'comment_id': '9001'},
    {'video_id': '101', 'comment_id': 'not-a-number'},
    {'video_id': '101', 'comment_id': '9001', 'cursor': -1},
    {'video_id': '101', 'comment_id': '9001', 'cursor': True},
    {'video_id': '101', 'comment_id': '9001', 'count': 0},
    {'video_id': '101', 'comment_id': '9001', 'count': 51},
])
def test_video_sub_comments_request_validation(client, payload):
    response = client.post('/api/v1/douyin/video_sub_comments', json=payload)

    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_REQUEST'


@pytest.mark.parametrize('upstream_response', [
    {'status_code': 0},
    {'comments': {}, 'cursor': 0, 'has_more': 0},
    {'comments': [], 'cursor': True, 'has_more': 0},
    {'comments': [], 'cursor': -1, 'has_more': 0},
    {'comments': [], 'cursor': 0, 'has_more': 2},
])
def test_video_sub_comments_rejects_malformed_upstream(client, fake_api, upstream_response):
    fake_api.sub_comment_response = upstream_response

    response = client.post('/api/v1/douyin/video_sub_comments', json={
        'video_id': '101',
        'comment_id': '9001',
    })

    assert response.status_code == 502
    assert response.json()['error']['code'] == 'UPSTREAM_ERROR'


def test_work_comments_malformed_upstream_returns_502(client, fake_api):
    fake_api.comment_response = {'status_code': 0}

    response = client.post('/api/v1/douyin/video_comments', json={
        'url': 'https://www.douyin.com/video/101',
    })

    assert response.status_code == 502
    assert response.json()['error']['code'] == 'UPSTREAM_ERROR'


@pytest.mark.parametrize('upstream_response', [
    {'comments': {}, 'cursor': 0, 'has_more': 0},
    {'comments': [], 'cursor': True, 'has_more': 0},
    {'comments': [], 'cursor': -1, 'has_more': 0},
    {'comments': [], 'cursor': 0, 'has_more': 2},
    {'comments': [], 'cursor': 0, 'has_more': 1.0},
])
def test_work_comments_rejects_invalid_pagination_response(client, fake_api, upstream_response):
    fake_api.comment_response = upstream_response

    response = client.post('/api/v1/douyin/video_comments', json={
        'url': 'https://www.douyin.com/video/101',
    })

    assert response.status_code == 502
    assert response.json()['error']['code'] == 'UPSTREAM_ERROR'


@pytest.mark.parametrize('payload', [
    {},
    {'url': 'https://www.douyin.com/video/101', 'video_id': '101'},
    {'video_id': 'not-a-number'},
    {'url': 'http://www.douyin.com/video/101'},
    {'url': 'https://www.douyin.com/video/not-a-number'},
    {'url': 'https://www.douyin.com/video/101', 'cursor': -1},
    {'url': 'https://www.douyin.com/video/101', 'cursor': True},
    {'url': 'https://www.douyin.com/video/101', 'count': 0},
    {'url': 'https://www.douyin.com/video/101', 'count': 51},
    {'url': 'https://www.douyin.com/video/101', 'count': 1.5},
])
def test_work_comments_request_validation(client, payload):
    response = client.post('/api/v1/douyin/video_comments', json=payload)

    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_REQUEST'


@pytest.mark.parametrize('payload', [
    {},
    {'urls': ['https://www.douyin.com/video/1'], 'video_id': '1'},
    {'video_id': 'not-a-number'},
    {'urls': []},
    {'urls': ['https://example.com/video/1']},
    {'urls': ['http://www.douyin.com/video/1']},
    {'urls': ['https://www.douyin.com/user/abc']},
])
def test_works_request_validation(client, payload):
    response = client.post('/api/v1/douyin/video_info', json=payload)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_REQUEST'


def test_video_info_accepts_video_id(client):
    response = client.post('/api/v1/douyin/video_info', json={
        'urls': None,
        'video_id': '101',
    })
    body = response.json()

    assert response.status_code == 200
    assert body['data']['total'] == 1
    assert body['data']['items'][0]['work_url'] == 'https://www.douyin.com/video/101'


@pytest.mark.parametrize('url', [
    'https://www.douyin.com/video/not-a-number',
    'https://www.douyin.com/discover?modal_id=abc',
])
def test_invalid_work_id_is_a_422_parameter_error(client, url):
    response = client.post('/api/v1/douyin/video_info', json={'urls': [url]})

    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_REQUEST'


def test_overlong_urls_are_rejected(client):
    response = client.post('/api/v1/douyin/video_info', json={
        'urls': [f'https://www.douyin.com/video/101?payload={"x" * 2100}'],
    })
    user_response = client.post('/api/v1/douyin/user_videos', json={
        'user_url': f'https://www.douyin.com/user/account?payload={"x" * 2100}',
    })

    assert response.status_code == 422
    assert user_response.status_code == 422


@pytest.mark.parametrize('user_url', [
    'https://www.douyin.com/foo/user/',
    'https://www.douyin.com/user/',
    'https://www.douyin.com/user/account/extra',
    'http://www.douyin.com/user/account',
])
def test_invalid_user_homepage_is_a_422_parameter_error(client, user_url):
    response = client.post('/api/v1/douyin/user_videos', json={'user_url': user_url})

    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_REQUEST'


def test_user_works_returns_normalized_data(client):
    response = client.post('/api/v1/douyin/user_videos', json={
        'user_url': 'https://www.douyin.com/user/sec-user',
        'page_num': 2,
    })
    body = response.json()
    assert response.status_code == 200
    assert body['data']['total'] == 2
    assert body['data']['items'][0]['nickname'] == '主页昵称'
    assert body['data']['items'][0]['follower_count'] == 88


def test_user_works_accepts_user_id(client, fake_api):
    response = client.post('/api/v1/douyin/user_videos', json={
        'user_url': None,
        'user_id': 'sec-user',
        'page_num': 2,
    })
    body = response.json()

    assert response.status_code == 200
    assert body['data']['total'] == 2
    assert body['data']['user_url'] == 'https://www.douyin.com/user/sec-user'
    assert fake_api.user_work_args[1:] == ('https://www.douyin.com/user/sec-user', 2)
    # user_id 应直接定位用户，不能额外请求作品详情。
    assert fake_api.work_info_urls == []


@pytest.mark.parametrize('payload', [
    {},
    {'user_url': 'https://www.douyin.com/user/sec-user', 'user_id': 'sec-user'},
    {'user_id': 'invalid/user'},
    {'video_id': '101'},
])
def test_user_works_locator_validation(client, payload):
    response = client.post('/api/v1/douyin/user_videos', json=payload)

    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_REQUEST'


def test_search_works_passes_filters(client, fake_api):
    payload = {
        'query': ' 榴莲 ',
        'limit': 12,
        'sort_type': '2',
        'publish_time': '7',
        'filter_duration': '1-5',
        'search_range': '0',
        'content_type': '1',
    }
    response = client.post('/api/v1/douyin/search_videos', json=payload)
    assert response.status_code == 200
    assert response.json()['data']['query'] == '榴莲'
    # 第一个参数为服务持有的 auth
    assert fake_api.search_args[1:] == ('榴莲', 12, '2', '7', '1-5', '0', '1', True)
    assert response.json()['data'] == {
        'items': [response.json()['data']['items'][0]],
        'total': 1,
        'query': '榴莲',
        'has_more': True,
        'raw_page_counts': [25],
        'account_id': 'default',
        'failover_count': 0,
    }


@pytest.mark.parametrize('query', ['正常词\n伪造日志', '正常词\x1b[31mERROR'])
def test_search_query_rejects_log_control_characters(client, query):
    response = client.post('/api/v1/douyin/search_videos', json={'query': query})

    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_REQUEST'


def test_search_query_value_is_not_written_to_logs(client, capfd):
    secret_query = 'confidential-search-keyword'
    response = client.post('/api/v1/douyin/search_videos', json={'query': secret_query})
    captured = capfd.readouterr()

    assert response.status_code == 200
    assert secret_query not in captured.out
    assert secret_query not in captured.err


def test_search_risk_control_returns_sanitized_429(client, fake_api, monkeypatch, capfd):
    secret_query = 'confidential-risk-query'
    secret_body = 'private-upstream-response'

    def raise_risk(*args):
        raise DouyinRiskControlError('business_risk_signal')

    monkeypatch.setattr(fake_api, 'search_some_general_work', raise_risk)
    response = client.post('/api/v1/douyin/search_videos', json={'query': secret_query})
    captured = capfd.readouterr()

    assert response.status_code == 429
    assert response.json()['error'] == {
        'code': 'UPSTREAM_RISK_CONTROL',
        'message': '抖音上游触发访问限制或安全验证',
        'details': {'signal': 'business_risk_signal'},
    }
    assert secret_query not in response.text
    assert secret_body not in response.text
    assert secret_query not in captured.out
    assert secret_query not in captured.err


def test_api_path_does_not_write_files(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    response = client.post('/api/v1/douyin/video_info', json={
        'urls': ['https://www.douyin.com/video/101'],
    })
    comments_response = client.post('/api/v1/douyin/video_comments', json={
        'url': 'https://www.douyin.com/video/101',
    })
    assert response.status_code == 200
    assert comments_response.status_code == 200
    assert list(tmp_path.iterdir()) == []


def test_empty_account_pool_starts_but_scraping_returns_503():
    service = SpiderService(auth=None, max_concurrent=2, douyin_api=FakeDouyinAPI())
    with TestClient(main.create_app(service)) as test_client:
        health = test_client.get('/api/health')
        response = test_client.post('/api/v1/douyin/video_info', json={
            'urls': ['https://www.douyin.com/video/101'],
        })
        comments_response = test_client.post('/api/v1/douyin/video_comments', json={
            'url': 'https://www.douyin.com/video/101',
        })

    assert health.status_code == 200
    assert health.json()['data']['status'] == 'not_authenticated'
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'NO_AVAILABLE_ACCOUNT'
    assert comments_response.status_code == 503
    assert comments_response.json()['error']['code'] == 'NO_AVAILABLE_ACCOUNT'


def test_authentication_failure_503_contains_safe_retry_after():
    class ExpiredAPI:
        def get_work_info(self, auth, url):
            raise DouyinAuthenticationError('expired')

    auth = DouyinAuth()
    auth.perepare_auth('sessionid=test; s_v_web_id=fp-test', '', '')
    pool = AccountPool([
        CredentialRecord(1, 'account-a', datetime.now(timezone.utc), auth),
    ], cooldown_seconds=20)
    service = SpiderService(account_pool=pool, douyin_api=ExpiredAPI())

    with TestClient(main.create_app(service)) as test_client:
        response = test_client.post('/api/v1/douyin/video_info', json={
            'urls': ['https://www.douyin.com/video/101'],
        })

    assert response.status_code == 503
    assert response.json()['error']['code'] == 'NO_AVAILABLE_ACCOUNT'
    assert response.json()['error']['details']['retry_after_seconds'] in (19, 20)


def test_database_connection_failure_prevents_startup(monkeypatch):
    class FailingStore:
        def __init__(self):
            self.closed = False

        def check_connection(self):
            raise CredentialStoreError('sensitive database detail')

        def close(self):
            self.closed = True

    store = FailingStore()
    monkeypatch.setattr(
        main.MySQLCredentialStore,
        'from_env',
        classmethod(lambda cls: store),
    )
    with pytest.raises(CredentialStoreError):
        with TestClient(main.create_app()):
            pass
    assert store.closed is True


def test_latest_account_load_failure_still_closes_owned_store(monkeypatch):
    class FailingStore:
        def __init__(self):
            self.closed = False

        def check_connection(self):
            return True

        def load_latest(self):
            raise CredentialStoreError('load failed')

        def close(self):
            self.closed = True

    store = FailingStore()
    monkeypatch.setattr(
        main.MySQLCredentialStore,
        'from_env',
        classmethod(lambda cls: store),
    )

    with pytest.raises(CredentialStoreError):
        with TestClient(main.create_app()):
            pass
    assert store.closed is True


def test_database_empty_pool_allows_api_to_start(monkeypatch):
    class EmptyStore:
        def __init__(self):
            self.closed = False

        def check_connection(self):
            return True

        def load_latest(self):
            return []

        def close(self):
            self.closed = True

    store = EmptyStore()
    monkeypatch.setattr(
        main.MySQLCredentialStore,
        'from_env',
        classmethod(lambda cls: store),
    )
    with TestClient(main.create_app()) as test_client:
        response = test_client.get('/api/health')

    assert response.status_code == 200
    assert response.json()['data']['status'] == 'not_authenticated'
    assert response.json()['data']['database'] == 'ok'
    assert store.closed is True


def test_environment_is_loaded_before_store_initialization(monkeypatch):
    events = []

    class EmptyStore:
        def check_connection(self):
            return True

        def load_latest(self):
            return []

        def close(self):
            events.append('closed')

    def load_config():
        events.append('environment')

    def create_store(cls):
        assert events == ['environment']
        events.append('store')
        return EmptyStore()

    monkeypatch.setattr(main, 'load_environment', load_config)
    monkeypatch.setattr(main.MySQLCredentialStore, 'from_env', classmethod(create_store))

    with TestClient(main.create_app()) as test_client:
        assert test_client.get('/api/health').status_code == 200

    assert events == ['environment', 'store', 'closed']


def test_concurrency_is_limited_to_two():
    class CountingAPI(FakeDouyinAPI):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()

        def get_work_info(self, auth, url):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            time.sleep(0.03)
            with self.lock:
                self.active -= 1
            return {'aweme_detail': make_work(url.rsplit('/', 1)[-1])}

    api = CountingAPI()
    service = SpiderService(auth=object(), max_concurrent=2, douyin_api=api)

    def call(index):
        return service.get_works([f'https://www.douyin.com/video/{index}'], str(index))

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(call, range(6)))

    assert len(results) == 6
    assert api.maximum == 2


def test_work_url_query_is_not_written_to_logs(client, capfd):
    secret = 'private-query-value'
    response = client.post('/api/v1/douyin/video_info', json={
        'urls': [f'https://www.douyin.com/video/101?access_token={secret}'],
    })
    captured = capfd.readouterr()

    assert response.status_code == 200
    assert secret not in response.text
    assert secret not in captured.out
    assert secret not in captured.err


def test_failed_work_url_query_is_not_reflected_in_response(client):
    secret = 'private-query-value'
    response = client.post('/api/v1/douyin/video_info', json={
        'urls': [f'https://www.douyin.com/video/999?access_token={secret}'],
    })

    assert response.status_code == 502
    assert secret not in response.text


def test_api_loguru_sink_writes_to_stdout_only():
    marker = 'api-stdout-marker'
    completed = subprocess.run(
        [
            sys.executable,
            '-c',
            f"import main; from loguru import logger; logger.info('{marker}')",
        ],
        cwd=Path(main.__file__).resolve().parent,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )

    assert marker in completed.stdout
    assert marker not in completed.stderr


def test_prod_logging_writes_to_configured_directory(tmp_path, monkeypatch):
    """生产环境日志同时写入指定的持久化目录。"""
    monkeypatch.setenv('LOG_DIR', str(tmp_path))
    try:
        main.configure_logging('prod')
        main.logger.info('prod-file-log-marker')
        logging.getLogger('uvicorn.error').warning('uvicorn-file-log-marker')
        main.logger.complete()

        log_files = list(tmp_path.glob('douyin-spider-*.log'))
        assert len(log_files) == 1
        log_file = log_files[0]
        assert log_file.is_file()
        assert 'prod-file-log-marker' in log_file.read_text(encoding='utf-8')
        assert 'uvicorn-file-log-marker' in log_file.read_text(encoding='utf-8')
    finally:
        main.configure_logging('dev')

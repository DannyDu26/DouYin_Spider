# coding=utf-8
from types import SimpleNamespace

import pytest

from utils import dy_util, mstoken
from utils.http_util import get_douyin_http_timeout, get_douyin_tls_verify


def test_douyin_http_timeout_defaults(monkeypatch):
    monkeypatch.delenv('DOUYIN_CONNECT_TIMEOUT_SECONDS', raising=False)
    monkeypatch.delenv('DOUYIN_READ_TIMEOUT_SECONDS', raising=False)

    assert get_douyin_http_timeout() == (10.0, 30.0)


def test_douyin_http_timeout_accepts_positive_numbers(monkeypatch):
    monkeypatch.setenv('DOUYIN_CONNECT_TIMEOUT_SECONDS', '1.25')
    monkeypatch.setenv('DOUYIN_READ_TIMEOUT_SECONDS', '8')

    assert get_douyin_http_timeout() == (1.25, 8.0)


@pytest.mark.parametrize('value', ['', '0', '-1', 'nan', 'inf', '-inf', 'invalid'])
@pytest.mark.parametrize(
    'name',
    ['DOUYIN_CONNECT_TIMEOUT_SECONDS', 'DOUYIN_READ_TIMEOUT_SECONDS'],
)
def test_douyin_http_timeout_rejects_invalid_values(monkeypatch, name, value):
    monkeypatch.setenv('DOUYIN_CONNECT_TIMEOUT_SECONDS', '10')
    monkeypatch.setenv('DOUYIN_READ_TIMEOUT_SECONDS', '30')
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=name):
        get_douyin_http_timeout()


def test_generate_webid_passes_shared_timeout(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text=r'\"user_unique_id\":\"123456\"')

    monkeypatch.setenv('DOUYIN_CONNECT_TIMEOUT_SECONDS', '2.5')
    monkeypatch.setenv('DOUYIN_READ_TIMEOUT_SECONDS', '9.5')
    monkeypatch.delenv('DOUYIN_CA_BUNDLE', raising=False)
    monkeypatch.setattr(dy_util.requests, 'get', fake_get)

    assert dy_util.generate_webid(url='https://www.douyin.com/discover') == '123456'
    assert captured['timeout'] == (2.5, 9.5)
    assert captured['verify'] is True


@pytest.mark.parametrize('url', [
    'http://www.douyin.com/video/123',
    'https://example.com/video/123',
])
def test_generate_webid_never_sends_cookie_to_untrusted_url(monkeypatch, url):
    requested = []

    def fake_get(*args, **kwargs):
        requested.append((args, kwargs))
        raise AssertionError('不应访问非受信 URL')

    auth = SimpleNamespace(cookie_str='sessionid=SECRET')
    monkeypatch.setattr(dy_util.requests, 'get', fake_get)

    webid = dy_util.generate_webid(auth=auth, url=url)

    assert len(webid) == 19
    assert webid.isdigit()
    assert requested == []


def test_generate_webid_does_not_hide_invalid_timeout(monkeypatch):
    monkeypatch.setenv('DOUYIN_CONNECT_TIMEOUT_SECONDS', '0')
    monkeypatch.setenv('DOUYIN_READ_TIMEOUT_SECONDS', '30')

    with pytest.raises(RuntimeError, match='DOUYIN_CONNECT_TIMEOUT_SECONDS'):
        dy_util.generate_webid(url='https://www.douyin.com/discover')


def test_dynamic_mstoken_uses_selected_timeout_tls_and_not_legacy_cookie(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(headers={'x-ms-token': 'fresh-token'})

    monkeypatch.setenv('DOUYIN_CONNECT_TIMEOUT_SECONDS', '3')
    monkeypatch.setenv('DOUYIN_READ_TIMEOUT_SECONDS', '8')
    monkeypatch.delenv('DOUYIN_CA_BUNDLE', raising=False)
    monkeypatch.setenv('DY_COOKIES', 'ttwid=legacy-secret')
    monkeypatch.setattr(mstoken, 'build_report_body', lambda: 'body')
    monkeypatch.setattr(mstoken.requests, 'post', fake_post)

    token = mstoken.get_mstoken(ttwid='', use_cache=False)

    assert token == 'fresh-token'
    assert captured['headers']['cookie'] == ''
    assert captured['timeout'] == (3.0, 8.0)
    assert captured['verify'] is True


def test_douyin_tls_verify_defaults_to_system_ca(monkeypatch):
    monkeypatch.delenv('DOUYIN_CA_BUNDLE', raising=False)

    assert get_douyin_tls_verify() is True


def test_douyin_tls_verify_accepts_existing_file(monkeypatch, tmp_path):
    ca_bundle = tmp_path / 'internal-ca.pem'
    ca_bundle.write_text('test certificate', encoding='utf-8')
    monkeypatch.setenv('DOUYIN_CA_BUNDLE', str(ca_bundle))

    assert get_douyin_tls_verify() == str(ca_bundle.resolve())


@pytest.mark.parametrize('kind', ['missing', 'directory'])
def test_douyin_tls_verify_rejects_non_file(monkeypatch, tmp_path, kind):
    ca_bundle = tmp_path / 'ca-target'
    if kind == 'directory':
        ca_bundle.mkdir()
    monkeypatch.setenv('DOUYIN_CA_BUNDLE', str(ca_bundle))

    with pytest.raises(RuntimeError, match='DOUYIN_CA_BUNDLE'):
        get_douyin_tls_verify()

"""按账号复用用户主页读取会话，统一设备号、Cookie 和浏览器指纹。"""
import os
import re
import threading

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.impersonate import BrowserType

from utils.dy_util import generate_fake_webid
from utils.http_util import get_douyin_http_timeout, get_douyin_tls_verify
from .headers import HeaderBuilder, HeaderType
from .requests import DouyinAPI as ReadRequests


_client_lock = threading.Lock()


def _impersonate():
    """与成功项目相同，选择已安装运行库支持的 Chrome 指纹。"""
    requested = (os.getenv('DY_HTTP_IMPERSONATE') or 'chrome151').strip().lower()
    supported = {item.value for item in BrowserType}
    if requested in supported:
        return requested
    match = re.fullmatch(r'chrome(\d+)', requested)
    if match:
        candidates = sorted(
            int(value[6:]) for value in supported
            if re.fullmatch(r'chrome\d+', value) and int(value[6:]) <= int(match[1])
        )
        if candidates:
            return f'chrome{candidates[-1]}'
    return 'chrome'


class UserReadClient:
    def __init__(self, auth):
        self.auth = auth
        self.session = requests.Session()
        self.lock = threading.RLock()
        self._webid = ''
        self._resolving_webid = False
        self._seeded = {}

    @property
    def cookie(self):
        return self.auth.cookie or {}

    @property
    def msToken(self):
        return self.auth.msToken

    @property
    def sec_uid(self):
        return getattr(self.auth, 'sec_uid', None) or getattr(self.auth, '_sec_uid', None)

    @property
    def webid(self):
        with self.lock:
            if self._webid:
                return self._webid
            if self._resolving_webid:
                return generate_fake_webid()
            self._resolving_webid = True
            try:
                # 先换取真实设备号；引导请求内部使用临时 ID，避免递归。
                from dy_apis.douyin_api import parse_douyin_response
                data = parse_douyin_response(ReadRequests.get_device_id(self))
                self._webid = str(data.get('id') or '')
            except (RuntimeError, ValueError, KeyError, RequestException):
                self._webid = ''
            finally:
                self._resolving_webid = False
            if not self._webid:
                headers = HeaderBuilder.build(HeaderType.DOC).get()
                headers.pop('cookie', None)
                try:
                    response = self.request('https://www.douyin.com/discover', headers)
                    matches = re.findall(r'\\"user_unique_id\\":\\"(.*?)\\"', response.text)
                    self._webid = matches[0] if matches else ''
                except RequestException:
                    pass
            self._webid = self._webid or generate_fake_webid()
            return self._webid

    def request(self, url, headers, **kwargs):
        with self.lock:
            # 显式同步变更的 Cookie，避免 jar 和 cookies 参数重复发送。
            host_only = {
                's_v_web_id', '__ac_nonce', '__ac_signature', 'x-web-secsdk-uid',
                'dy_swidth', 'dy_sheight', 'device_web_cpu_core',
                'device_web_memory_size', 'architecture', 'fpk1', 'fpk2',
            }
            current = dict(self.cookie)
            for name in self._seeded.keys() - current.keys():
                domain = 'www.douyin.com' if name in host_only else '.douyin.com'
                self.session.cookies.delete(name, domain=domain, path='/')
            for name, value in current.items():
                if self._seeded.get(name) != value:
                    domain = 'www.douyin.com' if name in host_only else '.douyin.com'
                    self.session.cookies.set(name, value, domain=domain, path='/', secure=True)
            self._seeded = current
            response = self.session.get(
                url, headers=headers, impersonate=_impersonate(),
                default_headers=False, http_version='v2',
                timeout=get_douyin_http_timeout(), verify=get_douyin_tls_verify(),
                **kwargs,
            )
            # 将上游更新同步到原账号，不写入数据库或其他账号的凭证。
            fresh = response.cookies.get_dict()
            if fresh:
                self.auth.cookie.update(fresh)
                self.auth.cookie_str = '; '.join(f'{k}={v}' for k, v in self.cookie.items())
                self.auth._ttwid = self.cookie.get('ttwid', '')
            return response

    def get_user_info(self, user_url):
        with self.lock:
            return ReadRequests.get_user_info(self, user_url)

    def get_user_work_info(self, user_url, max_cursor):
        with self.lock:
            return ReadRequests.get_user_work_info(self, user_url, str(max_cursor))

    def close(self):
        with self.lock:
            self.session.close()


def get_user_read_client(auth):
    """惰性创建账号专属会话，账号凭证替换后自然使用新会话。"""
    with _client_lock:
        client = getattr(auth, '_user_read_client', None)
        if client is None:
            client = UserReadClient(auth)
            auth._user_read_client = client
        return client

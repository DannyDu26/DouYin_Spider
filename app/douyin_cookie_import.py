"""抖音浏览器登录校验、凭证保存与账号池更新。"""
import threading

from fastapi import Header
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.account_store import LOGIN_COOKIE_NAMES, deserialize_credential, validate_account_id
from app.browser_cookie_import import BrowserCookieImportError, parse_browser_cookie, require_import_token
from dy_apis.douyin_api import DouyinAPI, DouyinAuthenticationError, DouyinRiskControlError


class DouyinCookieImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_id: str = Field(min_length=1, max_length=64, description='自定义账号池别名，同一别名重新同步时更新凭证。', examples=['douyin_01'])
    cookie: str = Field(min_length=1, max_length=32768, repr=False,
                        description='完整抖音 Cookie，需登录会话与 s_v_web_id；不在响应中返回。', examples=['sessionid=替换为真实会话; s_v_web_id=替换为指纹'])

    @field_validator('account_id')
    @classmethod
    def validate_account(cls, value):
        return validate_account_id(value)

    @field_validator('cookie')
    @classmethod
    def validate_cookie(cls, value):
        cookies = parse_browser_cookie(value)
        if not any(cookies.get(name) for name in LOGIN_COOKIE_NAMES):
            raise ValueError('Cookie 缺少抖音登录字段')
        if not cookies.get('s_v_web_id'):
            raise ValueError('Cookie 缺少 s_v_web_id')
        return value


def require_douyin_import_token(x_douyin_import_token: str = Header(default='', description='抖音 Cookie 同步密钥')):
    require_import_token(x_douyin_import_token, 'DOUYIN')


class DouyinCookieImportService:
    def __init__(self, store, account_pool):
        self.store, self.pool = store, account_pool
        self._lock = threading.Lock()
        self._pending = set()
        self._slots = threading.BoundedSemaphore(2)

    def import_cookie(self, payload):
        # 同步接口在线程池运行，限制并发并阻止同一账号重复提交。
        with self._lock:
            if payload.account_id in self._pending:
                raise BrowserCookieImportError('DOUYIN_COOKIE_IMPORT_ACTIVE', '该账号正在同步，请稍后重试', 409)
            if not self._slots.acquire(blocking=False):
                raise BrowserCookieImportError('DOUYIN_COOKIE_IMPORT_BUSY', '同步任务繁忙，请稍后重试', 503)
            self._pending.add(payload.account_id)
        try:
            auth = deserialize_credential(payload.cookie)
            try:
                user_id = DouyinAPI.get_my_uid(auth)
                if isinstance(user_id, bool) or not str(user_id).isdigit() or int(user_id) <= 0:
                    raise DouyinAuthenticationError('未取得正式用户')
                auth.uid = int(user_id)
            except DouyinAuthenticationError as error:
                raise BrowserCookieImportError('DOUYIN_AUTH_EXPIRED', '抖音登录无效或已过期，请重新登录后同步', 401) from error
            except DouyinRiskControlError as error:
                raise BrowserCookieImportError('DOUYIN_VERIFICATION_REQUIRED', '抖音要求安全验证，请在平台完成验证后重试', 429) from error
            except Exception as error:
                raise BrowserCookieImportError('DOUYIN_COOKIE_VERIFY_FAILED', '抖音登录校验失败，请稍后重试', 502) from error
            try:
                # 验证通过后才更新数据库，写入成功后才替换内存账号。
                record = self.store.insert(payload.account_id, auth)
            except Exception as error:
                raise BrowserCookieImportError('DOUYIN_CREDENTIAL_SAVE_FAILED', '抖音凭证保存失败', 502) from error
            self.pool.upsert(payload.account_id, auth, record)
            return {'account_id': payload.account_id, 'status': 'succeeded'}
        finally:
            with self._lock:
                self._pending.discard(payload.account_id)
                self._slots.release()

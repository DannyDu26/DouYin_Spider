"""小红书账号执行、分页转换和状态快照。"""
import os
import math
import subprocess
import threading
import time

from loguru import logger
from sqlalchemy import inspect

from app.account_pool import AccountPool, NoAvailableAccountError, _positive_int_from_env
from app.account_store import CredentialStoreError, MySQLCredentialStore
from app.xhs_credentials import XHS_CREDENTIAL_TYPE, XhsCredentialCodec
from xhs_apis.errors import XhsApiError, XhsAuthenticationError, XhsRiskControlError
from xhs_apis.xhs_pc_apis import XHS_Apis
from xhs_apis.request_policy import XhsRequestPolicy
from xhs_apis.xhs_pc_login_apis import XHSLoginApi
from xhs_utils.xhs_pc.params import generate_search_id, generate_search_session_id
from xhs_utils.xhs_pc.state import DS_REFRESH_INTERVAL_MS, now_ms


def check_xhs_runtime():
    """启动时检查 Node 能力与 npm 依赖，不执行上游请求。"""
    root = os.path.dirname(os.path.dirname(__file__))
    try:
        subprocess.run(['node', '-e', "require('crypto-js'); if(typeof require('zlib').crc32 !== 'function') process.exit(2)"],
                       cwd=root, capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('小红书需要支持 zlib.crc32 的 Node.js 和 npm ci 安装的依赖') from error


def _xhs_project_id(douyin_store):
    # 小红书可使用独立项目；未配置时兼容旧的共享项目配置。
    return _positive_int_from_env('XHS_PROJECT_ID', douyin_store.project_id)


def create_xhs_service(douyin_store):
    # 复用已有连接池，只操作小红书类型的记录。
    check_xhs_runtime()
    columns = inspect(douyin_store.engine).get_columns('crawler_cookie')
    remark = next((column['type'] for column in columns if column['name'] == 'remark'), None)
    if remark is None or (getattr(remark, 'length', None) is not None and remark.length < 65535):
        raise RuntimeError('小红书 remark 容量不足，请先执行 docker/migrations/001_xhs_remark_text.sql')
    store = MySQLCredentialStore(douyin_store.engine, _xhs_project_id(douyin_store),
                                XHS_CREDENTIAL_TYPE, codec=XhsCredentialCodec())
    pool = AccountPool(
        store.load_latest(), max_concurrent_per_account=1,
        cooldown_seconds=_positive_int_from_env('XHS_ACCOUNT_COOLDOWN_SECONDS', 900),
        cooldown_failure_limit=0, manage_auth_resources=True,
    )
    return XhsService(pool, store)


class XhsService:
    def __init__(self, account_pool, credential_store=None, client_factory=XHS_Apis):
        self.account_pool = account_pool
        self.credential_store = credential_store
        self.client_factory = client_factory
        self.max_concurrent = _positive_int_from_env('XHS_MAX_CONCURRENT_REQUESTS', 1)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)
        self.request_policy = XhsRequestPolicy.from_env()
        self.snapshot_interval = _positive_int_from_env('XHS_SNAPSHOT_INTERVAL_SECONDS', 60)
        self._snapshot_times = {}

    @staticmethod
    def prepare_runtime(auth):
        """更新过期安全材料，保留正式会话，不调用访客 activate。"""
        cookies = auth.profile.cookie_map
        if cookies.get('websectiga') and auth.needs_tiga_refresh():
            from app.xhs_login_service import LoginHttpGuard
            login = XHSLoginApi(http_client=LoginHttpGuard(auth.http_client),
                                proxies=auth.proxies, dsl=auth.dsl, web_build=auth.profile.web_build)
            login.profile = auth.profile
            login._cookie_store = auth._cookie_store
            login.local_storage, login.session_storage = auth.profile.browser_storage_snapshot()
            formal_session = cookies['web_session']
            try:
                login._initialize_security(cookies)
                auth.dsl = login.dsl
            except XhsApiError:
                raise
            except Exception as error:
                raise XhsApiError('XHS_SECURITY_REFRESH_FAILED', '小红书安全上下文刷新失败') from error
            finally:
                # 安全接口返回值不能替换已登录的正式会话。
                cookies['web_session'] = formal_session
                auth.update_cookies(cookies)
            auth.login_verified = False
        if auth.dsl and now_ms() - auth.profile.session.dsllt >= DS_REFRESH_INTERVAL_MS:
            try:
                auth.refresh_dsl()
            except Exception as error:
                raise XhsApiError('XHS_SECURITY_REFRESH_FAILED', '小红书 DS 参数刷新失败') from error

    def _record(self, account_id, auth):
        with self.account_pool._condition:
            state = self.account_pool._accounts.get(account_id)
            return state.record if state and state.record.auth is auth else None

    def persist_snapshot(self, account_id, auth, force=False):
        """关键凭证立即保存；计数按间隔保存，写回带旧快照条件。"""
        if self.credential_store is None:
            return
        record = self._record(account_id, auth)
        if record is None:
            return
        previous = self.credential_store.codec.metadata(record.stored_remark)
        critical_changed = (
            auth.cookies != record.stored_cookie
            or auth.profile.local_storage.get('webSsk') != previous.get('local_storage', {}).get('webSsk')
            or auth._cookie_store.export_state() != previous.get('host_cookie_state', {})
        )
        last_saved = self._snapshot_times.get(account_id, 0)
        if not force and not critical_changed and time.monotonic() - last_saved < self.snapshot_interval:
            return
        try:
            updated = self.credential_store.save_snapshot(record, auth)
            if updated is not None:
                self.account_pool.update_snapshot(updated)
                self._snapshot_times[account_id] = time.monotonic()
        except (CredentialStoreError, ValueError) as error:
            # 快照异常不泄漏状态正文，也不覆盖已成功的抓取结果。
            logger.error('小红书状态保存失败 account_id={} error_type={}', account_id, type(error).__name__)

    def _execute(self, operation, request_id):
        # 有界等待取代立即重试；入口并发与账号槽位共用等待预算。
        deadline = time.monotonic() + self.request_policy.acquire_timeout
        if not self._semaphore.acquire(timeout=self.request_policy.acquire_timeout):
            raise self.request_policy.queue_full()
        excluded = set()
        last_auth_error = None
        try:
            for attempt in range(2):
                try:
                    with self.account_pool.acquire(exclude=excluded, timeout=max(0, deadline - time.monotonic())) as lease:
                        client = self.client_factory(lease.auth)
                        client.request_policy = self.request_policy
                        try:
                            self.prepare_runtime(lease.auth)
                            if not getattr(lease.auth, 'login_verified', False):
                                client.bootstrap()
                            result = operation(client)
                        except XhsAuthenticationError as error:
                            last_auth_error = error
                            lease.auth.login_verified = False
                            logger.warning('[{}] 小红书账号登录已过期，请重新登录 account_id={}',
                                           request_id, lease.account_id)
                            self.account_pool.remove_account(lease.account_id, lease.row_id, lease.auth)
                            excluded.add(lease.account_id)
                            if attempt == 1:
                                raise
                            continue
                        except XhsRiskControlError as error:
                            cooldown = max(self.account_pool.cooldown_seconds,
                                           (error.details or {}).get('retry_after_seconds', 0))
                            self.account_pool.mark_risk_control(lease.account_id, lease.row_id, lease.auth,
                                                                cooldown_seconds=cooldown)
                            logger.warning('[{}] 小红书账号风控，冷却秒数={} account_id={}',
                                           request_id, cooldown, lease.account_id)
                            error.details = dict(error.details or {}, retry_after_seconds=math.ceil(cooldown),
                                                 reason='account_risk_control')
                            raise
                        finally:
                            self.persist_snapshot(lease.account_id, lease.auth)
                        result.update(account_id=lease.account_id, failover_count=attempt)
                        return result
                except NoAvailableAccountError as error:
                    # 切换账号失败时保留本次请求已确认的登录过期原因。
                    if last_auth_error is not None:
                        raise last_auth_error from error
                    details = {'retry_after_seconds': error.retry_after_seconds} if error.retry_after_seconds else None
                    stats = self.account_pool.stats()
                    reason = ('accounts_cooling' if not stats['available'] and stats['cooling'] else
                              'account_busy' if stats['available'] else 'no_valid_accounts')
                    details = dict(details or {}, reason=reason)
                    if reason == 'account_busy':
                        details['retry_after_seconds'] = 1
                    raise XhsApiError('XHS_NO_AVAILABLE_ACCOUNT', '当前没有可用的小红书账号', 503, details) from error
        finally:
            self._semaphore.release()

    @staticmethod
    def _page(data, key):
        items = data.get(key, [])
        if not isinstance(items, list) or type(data.get('has_more')) is not bool:
            raise XhsApiError('XHS_INVALID_RESPONSE', '小红书分页响应结构异常')
        cursor = data.get('cursor')
        if data['has_more'] and cursor is None:
            raise XhsApiError('XHS_INVALID_RESPONSE', '小红书分页响应缺少 cursor')
        return {'items': items, 'next_cursor': str(cursor) if cursor is not None else '', 'has_more': data['has_more']}

    def note_info(self, payload, request_id):
        def operation(client):
            items = client.get_note_info(payload.note_url).get('items')
            if not isinstance(items, list) or not items or not isinstance(items[0], dict):
                raise XhsApiError('XHS_NOTE_UNAVAILABLE', '小红书上游未返回笔记详情')
            return {'note': items[0]}
        return self._execute(operation, request_id)

    def user_info(self, payload, request_id):
        return self._execute(lambda client: {'user': client.get_user_info(payload.user_id)}, request_id)

    def user_notes(self, payload, request_id):
        return self._execute(lambda client: self._page(client.get_user_note_info(
            payload.user_id, payload.cursor, payload.xsec_token, payload.xsec_source), 'notes'), request_id)

    def note_comments(self, payload, request_id):
        return self._execute(lambda client: self._page(client.get_note_out_comment(
            payload.note_id, payload.cursor, payload.xsec_token), 'comments'), request_id)

    def note_sub_comments(self, payload, request_id):
        return self._execute(lambda client: self._page(client.get_note_inner_comment(
            payload.note_id, payload.root_comment_id, payload.cursor, payload.xsec_token), 'comments'), request_id)

    def search_notes(self, payload, request_id):
        params = payload.model_dump()
        params['search_id'] = payload.search_id or generate_search_id()
        params['search_session_id'] = payload.search_session_id or generate_search_session_id()
        def operation(client):
            data = client.search_note(**params)
            if not isinstance(data.get('items', []), list) or type(data.get('has_more')) is not bool:
                raise XhsApiError('XHS_INVALID_RESPONSE', '小红书搜索响应结构异常')
            return {'items': data.get('items', []), 'page': payload.page,
                    'next_page': payload.page + 1 if data['has_more'] else None,
                    'has_more': data['has_more'], 'search_id': params['search_id'],
                    'search_session_id': params['search_session_id']}
        return self._execute(operation, request_id)

    def refresh(self):
        if self.credential_store:
            return self.account_pool.refresh(self.credential_store.load_latest())
        return 0

    def close(self):
        # 停服前保存空闲账号；正在执行的请求由自己的租约保存。
        for item in self.account_pool.list_accounts():
            try:
                with self.account_pool.acquire(account_id=item['account_id'], timeout=0) as lease:
                    self.persist_snapshot(lease.account_id, lease.auth, force=True)
            except NoAvailableAccountError:
                continue
        self.account_pool.close()

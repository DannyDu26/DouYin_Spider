"""纯 HTTP 短信与扫码登录，共享凭证保存和会话清理。"""
import asyncio
import base64
import inspect
from io import BytesIO
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger
import qrcode

from app.account_pool import _positive_int_from_env
from app.account_store import CredentialStoreError
from xhs_apis.errors import XhsApiError, XhsRiskControlError
from xhs_apis.request_policy import retry_after_seconds
from xhs_apis.xhs_pc_login_apis import XHSLoginApi
from xhs_apis.xhs_pc_apis import XHS_Apis
from xhs_utils.xhs_pc import XHSPcAuth


TERMINAL = {'succeeded', 'expired', 'failed', 'cancelled'}


class LoginHttpGuard:
    """保留登录传输行为，并将明确的安全验证信号交给会话层。"""
    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)

    def request(self, method, url, **kwargs):
        response = self.client.request(method, url, **kwargs)
        if response.status_code in {429, 461, 471}:
            raise XhsRiskControlError(f'http_{response.status_code}', retry_after_seconds(response))
        return response

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)


@dataclass
class SmsSession:
    session_id: str
    account_id: str
    phone: str
    zone: str
    created_at: float
    expires_at: float
    status: str = 'starting'
    error_code: str | None = None
    error_message: str | None = None
    error_status: int = 502
    error_details: dict | None = None
    terminal_at: float | None = None
    failed_attempts: int = 0
    login: object = None
    cookies: dict = field(default_factory=dict)
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    kind: str = 'sms'
    qr_id: str = ''
    qr_code: str = ''
    qrcode_data_url: str = ''


class XhsLoginService:
    def __init__(self, credential_store, account_pool, login_factory=XHSLoginApi):
        self.store = credential_store
        self.pool = account_pool
        self.login_factory = login_factory
        # 短信会话至少保留五分钟，避免旧部署的短超时配置提前作废验证码。
        self.timeout = max(300, _positive_int_from_env('XHS_SMS_SESSION_TIMEOUT_SECONDS', 300))
        self.retention = _positive_int_from_env('XHS_SMS_SESSION_RETENTION_SECONDS', 300)
        self.send_interval = _positive_int_from_env('XHS_SMS_RESEND_INTERVAL_SECONDS', 60)
        self.max_sessions = _positive_int_from_env('XHS_SMS_MAX_SESSIONS', 10)
        self.max_attempts = _positive_int_from_env('XHS_SMS_MAX_CODE_ATTEMPTS', 5)
        self.qr_timeout = _positive_int_from_env('XHS_QR_SESSION_TIMEOUT_SECONDS', 180)
        self.qr_poll_interval = 2.0
        self.sessions = {}
        self._last_send = {}
        self._tasks = set()
        self._workers = asyncio.Semaphore(2)
        self._cleanup_task = None
        self._closed = False

    async def _thread(self, function):
        # 取消时等线程退出，避免仍有请求使用已关闭的客户端。
        task = asyncio.create_task(asyncio.to_thread(function))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    def _view(session):
        def iso(value):
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        result = {'session_id': session.session_id, 'account_id': session.account_id,
                  'status': session.status, 'created_at': iso(session.created_at), 'expires_at': iso(session.expires_at)}
        if session.kind == 'sms':
            result['phone_masked'] = '*' * max(0, len(session.phone) - 4) + session.phone[-4:]
        if session.error_code:
            result['error'] = {'code': session.error_code, 'message': session.error_message}
        return result

    async def _dispose(self, session):
        if session.login is not None:
            login, session.login = session.login, None
            await self._thread(login.close)
        session.cookies.clear()
        session.qr_id = session.qr_code = session.qrcode_data_url = ''

    async def _finish(self, session, status):
        session.status = status
        session.terminal_at = time.time()
        await self._dispose(session)

    async def _prune(self):
        for session in list(self.sessions.values()):
            if session.status not in TERMINAL and time.time() >= session.expires_at and session.status != 'committing':
                session.status = 'expired'
                if session.task and not session.task.done():
                    session.task.cancel()
                    await asyncio.gather(session.task, return_exceptions=True)
                await self._finish(session, 'expired')
            if session.terminal_at and time.time() - session.terminal_at >= self.retention:
                self.sessions.pop(session.session_id, None)
        active_phones = {(item.zone, item.phone) for item in self.sessions.values() if item.status not in TERMINAL}
        for key, sent_at in list(self._last_send.items()):
            if key not in active_phones and time.monotonic() - sent_at >= self.send_interval:
                self._last_send.pop(key, None)

    async def _cleanup(self):
        while True:
            await asyncio.sleep(5)
            await self._prune()

    async def _get(self, session_id):
        await self._prune()
        session = self.sessions.get(session_id)
        if session is None:
            raise XhsApiError('XHS_LOGIN_SESSION_NOT_FOUND', '登录会话不存在或已过保留期', 404)
        return session

    def _launch(self, session, status, operation, next_status):
        session.status = status
        session.error_code = session.error_message = None
        session.error_status = 502
        session.error_details = None
        task = asyncio.create_task(self._run(session, operation, next_status))
        session.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, session, operation, next_status):
        try:
            if inspect.iscoroutinefunction(operation):
                # 扫码等待释放网络并发槽，避免阻塞短信登录。
                result = await operation()
            else:
                async with self._workers:
                    result = await self._thread(operation)
            if session.status in TERMINAL or time.time() >= session.expires_at:
                if isinstance(result, XHSPcAuth):
                    await self._thread(result.close)
                await self._finish(session, 'expired' if session.status not in TERMINAL else session.status)
                return
            if next_status == 'succeeded':
                # 提交阶段不可取消；数据库成功后才接管客户端。
                session.status = 'committing'
                record = await self._thread(lambda: self.store.insert(session.account_id, result))
                self.pool.upsert(record)
                session.login = None
                await self._finish(session, 'succeeded')
            else:
                session.status = next_status
        except asyncio.CancelledError:
            await self._dispose(session)
            raise
        except Exception as error:
            # 验证码错误允许重试，其他异常只暴露预定义错误信息。
            session.error_code = error.code if isinstance(error, XhsApiError) else (
                'XHS_CREDENTIAL_SAVE_FAILED' if isinstance(error, CredentialStoreError) else 'XHS_LOGIN_FAILED')
            session.error_message = error.message if isinstance(error, XhsApiError) else '小红书登录执行失败'
            session.error_status = error.status_code if isinstance(error, XhsApiError) else 502
            session.error_details = error.details if isinstance(error, XhsApiError) else None
            if session.error_status == 410:
                await self._finish(session, 'expired')
            elif isinstance(error, XhsRiskControlError) and session.kind != 'cookie':
                session.status = 'verification_required'
            elif session.error_code == 'XHS_SMS_INVALID_CODE' and session.failed_attempts < self.max_attempts:
                session.status = 'waiting_sms_code'
            elif session.error_code == 'XHS_SMS_SEND_FAILED':
                session.status = 'ready'
            else:
                await self._finish(session, 'failed')
            logger.warning('小红书登录步骤失败 session_id={} code={}', session.session_id, session.error_code)

    async def _reserve_session(self, account_id, phone='', zone='', kind='sms'):
        """两种登录共享账号冲突检查和总会话限制。"""
        if self._closed:
            raise XhsApiError('XHS_LOGIN_UNAVAILABLE', '小红书登录服务已关闭', 503)
        await self._prune()
        active = [item for item in self.sessions.values() if item.status not in TERMINAL]
        if any(item.account_id == account_id or (phone and (item.zone, item.phone) == (zone, phone)) for item in active):
            raise XhsApiError('XHS_SMS_SESSION_ACTIVE', '该账号或手机号已有进行中的登录会话', 409)
        if len(active) >= self.max_sessions:
            raise XhsApiError('XHS_SMS_SESSION_LIMIT', '登录会话数量已满', 503)
        now = time.time()
        session = SmsSession(uuid.uuid4().hex, account_id, phone, zone, now,
                             now + (self.qr_timeout if kind == 'qr' else self.timeout), kind=kind)
        self.sessions[session.session_id] = session
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        return session

    def _initialize_client(self, session):
        """初始化独立客户端，统一捕获上游安全验证信号。"""
        session.login = self.login_factory()
        if callable(getattr(session.login.http, 'request', None)):
            session.login.http = LoginHttpGuard(session.login.http)
        session.cookies = session.login.generate_init_cookies()

    def _authenticated_client(self, session):
        """正式用户确认成功后，接管完整设备和签名状态。"""
        success, user, cookies = session.login.get_user_info(session.cookies)
        if not success or user.get('guest') is not False or not user.get('user_id'):
            raise XhsApiError('XHS_LOGIN_NOT_AUTHENTICATED', '未取得正式登录用户')
        auth = XHSPcAuth.from_login_client(session.login, cookies, user['user_id'],
                                        login_source='qrcode' if session.kind == 'qr' else 'phone')
        auth.login_verified = True
        return auth

    async def import_cookie(self, payload):
        """验证浏览器 Cookie 后复用统一持久化、入池和失败清理流程。"""
        session = await self._reserve_session(payload.account_id, kind='cookie')

        def authenticate():
            session.login = XHSPcAuth.from_saved_state(payload.cookie)
            XHS_Apis(session.login).bootstrap()
            return session.login

        self._launch(session, 'starting', authenticate, 'succeeded')
        return await self._wait_for_step(session.session_id, 'succeeded')

    async def create_session(self, payload):
        session = await self._reserve_session(payload.account_id, payload.phone, payload.zone)
        def initialize():
            self._initialize_client(session)
            session.login.ensure_webprofile(session.cookies)
        self._launch(session, 'starting', initialize, 'ready')
        return self._view(session)

    async def get_session(self, session_id):
        return self._view(await self._get(session_id))

    async def _wait_for_step(self, session_id, expected_status):
        """等待现有后台任务，客户端断开不会中断凭证提交。"""
        session = await self._get(session_id)
        task = session.task
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling() or not task.cancelled():
                    raise
        session = await self._get(session_id)
        if session.status == expected_status:
            return self._view(session)
        details = dict(session.error_details or {})
        details.update(session_id=session_id, status=session.status)
        if session.status in {'expired', 'cancelled'}:
            raise XhsApiError(session.error_code or 'XHS_LOGIN_SESSION_ENDED',
                              session.error_message or '登录会话已过期或取消，请重新开始登录', 410, details)
        raise XhsApiError(session.error_code or 'XHS_SMS_STATE_CONFLICT',
                          session.error_message or '当前会话状态无法完成该步骤',
                          session.error_status if session.error_code else 409, details)

    async def send_sms(self, payload):
        """合并初始化和发送，重发复用同一手机号与账号的会话。"""
        if self._closed:
            raise XhsApiError('XHS_LOGIN_UNAVAILABLE', '小红书登录服务已关闭', 503)
        await self._prune()
        session = next((item for item in self.sessions.values()
                        if item.status not in TERMINAL and item.account_id == payload.account_id
                        and (item.zone, item.phone) == (payload.zone, payload.phone)), None)
        if session is None:
            created = await self.create_session(payload)
            session = self.sessions[created['session_id']]
            await self._wait_for_step(session.session_id, 'ready')
        try:
            await self.send_code(session.session_id)
        except XhsApiError as error:
            # 重发或并发冲突也返回会话标识，方便继续使用和排查。
            details = dict(error.details or {})
            details.update(session_id=session.session_id, status=session.status)
            raise XhsApiError(error.code, error.message, error.status_code, details) from error
        return await self._wait_for_step(session.session_id, 'waiting_sms_code')

    async def login_sms(self, session_id, code):
        """提交后等待正式用户校验、数据库保存和账号入池。"""
        await self.verify_code(session_id, code)
        return await self._wait_for_step(session_id, 'succeeded')

    async def create_qrcode(self, account_id):
        """按浏览器冷启动顺序生成二维码，图片只在创建响应返回。"""
        session = await self._reserve_session(account_id, kind='qr')
        def initialize():
            self._initialize_client(session)
            success, _, data = session.login.generate_qrcode(session.cookies)
            if not success or not data or not all(data.get(key) for key in ('qr_id', 'code', 'qr_url')):
                raise XhsApiError('XHS_QR_CREATE_FAILED', '小红书登录二维码生成失败')
            session.cookies = data['cookies']
            session.qr_id, session.qr_code = data['qr_id'], data['code']
            # 先匿名轮询，再上报指纹；二维码在预检完成后才提供给调用方。
            success, message, session.cookies = session.login.check_qrcode_status(
                session.qr_id, session.qr_code, session.cookies)
            if success or message != '请扫描二维码':
                raise XhsApiError('XHS_QR_INVALID_INITIAL_STATE', '二维码初始化状态异常，请重新生成')
            session.login.ensure_webprofile(session.cookies)
            output = BytesIO()
            qrcode.make(data['qr_url']).save(output, format='PNG')
            session.qrcode_data_url = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
        self._launch(session, 'starting', initialize, 'waiting_scan')
        view = await self._wait_for_step(session.session_id, 'waiting_scan')
        view['qrcode_data_url'] = session.qrcode_data_url
        session.qrcode_data_url = ''
        return view

    async def login_qrcode(self, session_id):
        """服务端等待扫码和手机确认，完成后复用统一入池流程。"""
        session = await self._get(session_id)
        async with session.lock:
            if session.kind != 'qr':
                raise XhsApiError('XHS_QR_SESSION_REQUIRED', '请使用二维码接口返回的 session_id', 409)
            if session.status == 'succeeded':
                return self._view(session)
            if session.status in {'expired', 'cancelled', 'failed', 'verification_required'}:
                return await self._wait_for_step(session_id, 'succeeded')
            if session.status == 'starting':
                raise XhsApiError('XHS_QR_STATE_CONFLICT', '二维码尚未就绪', 409)
            if session.task and not session.task.done():
                task_started = True
            elif session.status == 'waiting_scan':
                task_started = False
            else:
                raise XhsApiError('XHS_QR_STATE_CONFLICT', '二维码尚未就绪', 409)
            if not task_started:
                async def wait_for_scan():
                    while time.time() < session.expires_at:
                        async with self._workers:
                            success, message, cookies = await self._thread(lambda: session.login.check_qrcode_status(
                                session.qr_id, session.qr_code, session.cookies))
                            session.cookies = cookies
                            if success:
                                return await self._thread(lambda: self._authenticated_client(session))
                        if message == '二维码已过期':
                            break
                        if message not in {'请扫描二维码', '请确认登录'}:
                            raise XhsApiError('XHS_QR_POLL_FAILED', '小红书二维码状态查询失败')
                        session.status = 'waiting_confirm' if message == '请确认登录' else 'waiting_scan'
                        await asyncio.sleep(min(self.qr_poll_interval, max(0, session.expires_at - time.time())))
                    raise XhsApiError('XHS_QR_EXPIRED', '二维码已过期，请重新生成', 410)
                self._launch(session, 'waiting_scan', wait_for_scan, 'succeeded')
        return await self._wait_for_step(session_id, 'succeeded')

    async def send_code(self, session_id):
        session = await self._get(session_id)
        async with session.lock:
            if session.kind != 'sms' or session.status not in {'ready', 'waiting_sms_code'}:
                raise XhsApiError('XHS_SMS_STATE_CONFLICT', '当前会话状态不能发送验证码', 409)
            key = (session.zone, session.phone)
            remaining = self.send_interval - (time.monotonic() - self._last_send.get(key, -self.send_interval))
            if remaining > 0:
                raise XhsApiError('XHS_SMS_SEND_TOO_SOON', '请等待后再发送验证码', 429,
                                  {'retry_after_seconds': int(remaining) + 1})
            self._last_send[key] = time.monotonic()
            def send():
                success, _, result = session.login.send_phone_code(session.phone, session.cookies, session.zone)
                if not success:
                    raise XhsApiError('XHS_SMS_SEND_FAILED', '上游未接受短信发送请求')
            self._launch(session, 'sending_sms', send, 'waiting_sms_code')
            return self._view(session)

    async def verify_code(self, session_id, code):
        session = await self._get(session_id)
        async with session.lock:
            if session.kind != 'sms' or session.status != 'waiting_sms_code':
                raise XhsApiError('XHS_SMS_STATE_CONFLICT', '当前会话状态不能提交验证码', 409)
            session.failed_attempts += 1
            def verify():
                success, _, result = session.login.login_by_phone(session.phone, code, session.cookies, session.zone)
                if not success:
                    raise XhsApiError('XHS_SMS_INVALID_CODE', '验证码校验或登录失败，请检查验证码', 422)
                session.cookies = result['cookies']
                return self._authenticated_client(session)
            self._launch(session, 'verifying_sms', verify, 'succeeded')
            return self._view(session)

    async def cancel_session(self, session_id):
        session = await self._get(session_id)
        async with session.lock:
            if session.status == 'committing':
                raise XhsApiError('XHS_SMS_STATE_CONFLICT', '凭证正在提交，不能取消', 409)
            if session.status in TERMINAL:
                return self._view(session)
            session.status = 'cancelled'
            if session.task and not session.task.done():
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
            await self._finish(session, 'cancelled')
            return self._view(session)

    async def shutdown(self):
        self._closed = True
        if self._cleanup_task:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
        for session in list(self.sessions.values()):
            if session.status not in TERMINAL and session.status != 'committing':
                await self.cancel_session(session.session_id)
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

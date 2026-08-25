# coding=utf-8
"""抖音扫码登录会话管理。

二维码只保存在内存中，登录成功后先持久化凭证，再刷新内存账号池。
"""

import asyncio
import base64
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from dy_apis.login_api import BrowserVerificationRequiredError, DYLoginApi


ACTIVE_STATUSES = frozenset({
    'starting',
    'waiting_scan',
    'verification_required',
    'requesting_sms',
    'waiting_sms_code',
    'verifying_sms',
    'committing',
})
TERMINAL_STATUSES = frozenset({'succeeded', 'expired', 'failed', 'cancelled'})
ACCOUNT_ID_PATTERN = re.compile(r'^[a-z0-9_-]{1,64}$')
_TRUE_VALUES = frozenset({'1', 'true', 'yes', 'on'})
_FALSE_VALUES = frozenset({'0', 'false', 'no', 'off'})


class QrLoginServiceError(RuntimeError):
    """可由 HTTP 层安全转换的扫码会话异常。"""

    def __init__(self, code: str, message: str, status_code: int,
                 session_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.session_id = session_id


class QrSessionConflictError(QrLoginServiceError):
    def __init__(self, session_id: str):
        super().__init__(
            'QR_SESSION_ACTIVE',
            '已有扫码登录会话正在进行',
            409,
            session_id,
        )


class QrSessionNotFoundError(QrLoginServiceError):
    def __init__(self, session_id: str):
        super().__init__(
            'QR_SESSION_NOT_FOUND',
            '扫码登录会话不存在或已过保留期',
            404,
            session_id,
        )


@dataclass
class _QrSession:
    session_id: str
    account_id: str
    status: str
    created_at: datetime
    expires_at: datetime
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    qrcode_bytes: bytes | None = None
    refreshed_at: datetime | None = None
    terminal_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    persistence_started: bool = False
    # 页面操作必须留在 Playwright 所在任务内，通过队列传递指令。
    verification_commands: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None


def _positive_int_from_env(name: str, default: int) -> int:
    """读取正整数配置。"""
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f'{name} 必须是正整数') from error
    if value <= 0:
        raise RuntimeError(f'{name} 必须是正整数')
    return value


def _positive_number_from_env(name: str, default: float) -> float:
    """读取有限正数秒数。"""
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as error:
        raise RuntimeError(f'{name} 必须是有限正数') from error
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f'{name} 必须是有限正数')
    return value


def _boolean_from_env(name: str, default: bool) -> bool:
    """读取明确布尔配置，避免字符串 false 被当作真值。"""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeError(f'{name} 必须是 true/false、1/0、yes/no 或 on/off')


class QrLoginService:
    """管理单个活跃的 Playwright 扫码登录会话。"""

    def __init__(
            self,
            credential_store,
            account_pool,
            login_api: DYLoginApi | None = None,
            session_timeout_seconds: int | None = None,
            terminal_retention_seconds: int | None = None,
            qr_ready_timeout_seconds: int | None = None,
            persistence_timeout_seconds: float | None = None,
            sms_verification_timeout_seconds: int | None = None,
            headless: bool | None = None,
    ):
        self.credential_store = credential_store
        self.account_pool = account_pool
        self.login_api = login_api or DYLoginApi()
        self.session_timeout_seconds = (
            _positive_int_from_env('QR_SESSION_TIMEOUT_SECONDS', 180)
            if session_timeout_seconds is None else session_timeout_seconds
        )
        self.terminal_retention_seconds = (
            _positive_int_from_env('QR_SESSION_RETENTION_SECONDS', 300)
            if terminal_retention_seconds is None else terminal_retention_seconds
        )
        self.headless = (
            _boolean_from_env('QR_LOGIN_HEADLESS', True)
            if headless is None else headless
        )
        self.qr_ready_timeout_seconds = (
            (30 if self.headless else self.session_timeout_seconds)
            if qr_ready_timeout_seconds is None
            else qr_ready_timeout_seconds
        )
        self.persistence_timeout_seconds = (
            _positive_number_from_env('QR_PERSIST_TIMEOUT_SECONDS', 30.0)
            if persistence_timeout_seconds is None else persistence_timeout_seconds
        )
        self.sms_verification_timeout_seconds = (
            _positive_int_from_env('QR_SMS_VERIFICATION_TIMEOUT_SECONDS', 180)
            if sms_verification_timeout_seconds is None
            else sms_verification_timeout_seconds
        )
        self.debug_screenshot_enabled = _boolean_from_env(
            'QR_DEBUG_SCREENSHOT_ENABLED',
            False,
        )
        log_dir = os.getenv('LOG_DIR', '').strip() or os.path.join(
            os.getcwd(),
            'logs',
        )
        self.debug_screenshot_dir = (
            os.path.abspath(os.path.join(log_dir, 'qr-debug'))
            if self.debug_screenshot_enabled
            else None
        )
        if not isinstance(self.headless, bool):
            raise ValueError('headless 必须是布尔值')

        for name, value in (
                ('session_timeout_seconds', self.session_timeout_seconds),
                ('terminal_retention_seconds', self.terminal_retention_seconds),
                ('qr_ready_timeout_seconds', self.qr_ready_timeout_seconds),
                ('persistence_timeout_seconds', self.persistence_timeout_seconds),
                ('sms_verification_timeout_seconds', self.sms_verification_timeout_seconds),
        ):
            if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
            ):
                raise ValueError(f'{name} 必须是有限正数')

        self._sessions: dict[str, _QrSession] = {}
        self._active_session_id: str | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.isoformat().replace('+00:00', 'Z')

    @staticmethod
    def _credential_payload(auth) -> dict[str, Any]:
        """只组装持久化所需字段，不将其写入日志。"""
        cookie_str = getattr(auth, 'cookie_str', None)
        ticket = getattr(auth, 'ticket', None)
        private_key = getattr(auth, 'private_key', None)
        if not cookie_str or not ticket or not private_key:
            raise ValueError('扫码返回的登录凭证不完整')
        return {
            'version': 1,
            'cookie': cookie_str,
            'cookie_str': cookie_str,
            'ticket': ticket,
            'ts_sign': getattr(auth, 'ts_sign', None) or '',
            'client_cert': getattr(auth, 'client_cert', None) or '',
            'private_key': private_key,
        }

    @staticmethod
    def _public_session(session: _QrSession, include_qrcode: bool = False) -> dict:
        data = {
            'session_id': session.session_id,
            'account_id': session.account_id,
            'status': session.status,
            'created_at': QrLoginService._iso(session.created_at),
            'expires_at': QrLoginService._iso(session.expires_at),
        }
        if session.refreshed_at is not None:
            data['refreshed_at'] = QrLoginService._iso(session.refreshed_at)
        if session.error_code:
            data['error'] = {
                'code': session.error_code,
                'message': session.error_message,
            }
        if include_qrcode and session.qrcode_bytes:
            encoded = base64.b64encode(session.qrcode_bytes).decode('ascii')
            data['qrcode_data_url'] = f'data:image/png;base64,{encoded}'
        return data

    def _purge_expired_sessions_locked(self, now: datetime) -> None:
        """终态会话超过保留期后不再对外可见。"""
        retention = timedelta(seconds=self.terminal_retention_seconds)
        expired_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if session.terminal_at is not None and now - session.terminal_at >= retention
        ]
        for session_id in expired_ids:
            self._sessions.pop(session_id, None)

    async def _set_terminal(
            self,
            session: _QrSession,
            status: str,
            error_code: str | None = None,
            error_message: str | None = None,
    ) -> None:
        async with self._lock:
            # 保留先到达的终态，避免取消覆盖真实失败原因
            if session.status in TERMINAL_STATUSES:
                session.ready_event.set()
                return
            session.status = status
            session.error_code = error_code
            session.error_message = error_message
            session.terminal_at = self._now()
            session.ready_event.set()
            if self._active_session_id == session.session_id:
                self._active_session_id = None

    async def _qrcode_ready(self, session: _QrSession, qrcode_bytes: bytes) -> None:
        if not isinstance(qrcode_bytes, (bytes, bytearray)) or not qrcode_bytes:
            raise ValueError('二维码图片为空')
        async with self._lock:
            if session.status not in ACTIVE_STATUSES:
                return
            session.qrcode_bytes = bytes(qrcode_bytes)
            session.status = 'waiting_scan'
            session.ready_event.set()
        logger.info(
            '扫码登录二维码已就绪 session_id={} account_id={}',
            session.session_id,
            session.account_id,
        )

    async def _verification_updated(
            self,
            session: _QrSession,
            status: str,
            error_code: str | None = None,
            error_message: str | None = None,
    ) -> None:
        """接收浏览器身份验证阶段变化，不记录验证码等敏感内容。"""
        allowed_statuses = {
            'verification_required',
            'requesting_sms',
            'waiting_sms_code',
            'verifying_sms',
        }
        if status not in allowed_statuses:
            raise ValueError('未知的身份验证状态')
        async with self._lock:
            if session.status in TERMINAL_STATUSES or session.persistence_started:
                return
            session.status = status
            session.error_code = error_code
            session.error_message = error_message
            if status == 'verification_required':
                # 二次验证出现后重新计算到期时间，给收取短信和输入留足时间。
                sms_expires_at = self._now() + timedelta(
                    seconds=self.sms_verification_timeout_seconds,
                )
                session.expires_at = max(session.expires_at, sms_expires_at)
        logger.info(
            '扫码登录身份验证状态变化 session_id={} account_id={} status={}',
            session.session_id,
            session.account_id,
            status,
        )

    async def _persist_and_activate(self, session: _QrSession, auth) -> None:
        payload = self._credential_payload(auth)
        # 数据库写入放到工作线程；内存更新保持无 await，关闭超时竞态窗口。
        record = await asyncio.to_thread(
            self.credential_store.insert,
            session.account_id,
            payload,
        )
        self.account_pool.upsert(session.account_id, auth, record)

    async def _run_session(self, session: _QrSession) -> None:
        debug_screenshot_path = None
        if self.debug_screenshot_dir:
            # 每个会话只保留最新截图，避免调试文件持续增长。
            debug_screenshot_path = os.path.join(
                self.debug_screenshot_dir,
                f'qr-login-{session.session_id}-latest.png',
            )
        try:
            auth = await asyncio.wait_for(
                self.login_api.login_grab_ticket(
                    headless=self.headless,
                    timeout=self.session_timeout_seconds,
                    qrcode_callback=lambda data: self._qrcode_ready(session, data),
                    debug_screenshot_path=debug_screenshot_path,
                    verification_callback=lambda status, code=None, message=None: (
                        self._verification_updated(session, status, code, message)
                    ),
                    verification_command_queue=session.verification_commands,
                    verification_timeout=self.sms_verification_timeout_seconds,
                ),
                timeout=(
                    self.session_timeout_seconds
                    + self.sms_verification_timeout_seconds
                ),
            )
        except asyncio.CancelledError:
            await self._set_terminal(
                session,
                'cancelled',
                'QR_SESSION_CANCELLED',
                '扫码登录会话已取消',
            )
            logger.info(
                '扫码登录已取消 session_id={} account_id={}',
                session.session_id,
                session.account_id,
            )
            raise
        except (TimeoutError, asyncio.TimeoutError):
            await self._set_terminal(
                session,
                'expired',
                'QR_SESSION_EXPIRED',
                '扫码登录会话已超时',
            )
            logger.warning(
                '扫码登录超时 session_id={} account_id={}',
                session.session_id,
                session.account_id,
            )
            return
        except Exception as error:
            error_name = error.__class__.__name__
            error_text = str(error).lower()
            verification_required = isinstance(error, BrowserVerificationRequiredError)
            browser_unavailable = (
                'browserunavailable' in error_name.lower()
                or '未安装 chromium' in error_text
                or 'executable doesn\'t exist' in error_text
                or 'failed to launch' in error_text
            )
            if verification_required:
                error_code = 'QR_VERIFICATION_REQUIRED'
                error_message = '抖音要求浏览器验证，请使用可视模式手工完成'
            elif browser_unavailable:
                error_code = 'BROWSER_UNAVAILABLE'
                error_message = '浏览器不可用'
            else:
                error_code = 'QR_LOGIN_FAILED'
                error_message = '扫码登录失败'
            await self._set_terminal(
                session,
                'failed',
                error_code,
                error_message,
            )
            # 只记录异常类型，避免泄露凭证或连接信息
            logger.error(
                '扫码登录失败 session_id={} account_id={} error_type={}',
                session.session_id,
                session.account_id,
                error_name,
            )
            return

        async with self._lock:
            if session.status in TERMINAL_STATUSES:
                return
            # 提交阶段不可取消或并行开启下一次扫码。
            session.persistence_started = True
            session.status = 'committing'

        persistence_task = asyncio.create_task(
            self._persist_and_activate(session, auth),
            name=f'douyin-qr-persist-{session.session_id}',
        )
        try:
            try:
                # 超时仅告警，不取消不可回滚的 INSERT；最终状态以事务结果为准。
                await asyncio.wait_for(
                    asyncio.shield(persistence_task),
                    timeout=self.persistence_timeout_seconds,
                )
            except (TimeoutError, asyncio.TimeoutError):
                if not persistence_task.done():
                    logger.warning(
                        '登录凭证持久化较慢 session_id={} account_id={}',
                        session.session_id,
                        session.account_id,
                    )
                await persistence_task
        except asyncio.CancelledError:
            await self._set_terminal(
                session,
                'cancelled',
                'QR_SESSION_CANCELLED',
                '扫码登录会话已取消',
            )
            logger.info(
                '扫码登录已取消 session_id={} account_id={}',
                session.session_id,
                session.account_id,
            )
            raise
        except Exception as error:
            await self._set_terminal(
                session,
                'failed',
                'QR_LOGIN_FAILED',
                '扫码登录失败',
            )
            logger.error(
                '登录凭证持久化失败 session_id={} account_id={} error_type={}',
                session.session_id,
                session.account_id,
                error.__class__.__name__,
            )
            return

        async with self._lock:
            if session.status in TERMINAL_STATUSES:
                return
            session.status = 'succeeded'
            session.refreshed_at = self._now()
            session.terminal_at = session.refreshed_at
            session.ready_event.set()
            if self._active_session_id == session.session_id:
                self._active_session_id = None
        logger.info(
            '扫码登录成功 session_id={} account_id={}',
            session.session_id,
            session.account_id,
        )

    async def create_session(self, account_id: str) -> dict:
        """创建会话并等待二维码就绪。"""
        if not isinstance(account_id, str) or not ACCOUNT_ID_PATTERN.fullmatch(account_id):
            raise QrLoginServiceError(
                'INVALID_ACCOUNT_ID',
                'account_id 必须由 1-64 位小写字母、数字、_ 或 - 组成',
                422,
            )

        async with self._lock:
            if self._closed:
                raise QrLoginServiceError(
                    'QR_LOGIN_SERVICE_UNAVAILABLE',
                    '扫码登录服务已停止',
                    503,
                )
            now = self._now()
            self._purge_expired_sessions_locked(now)
            if self._active_session_id:
                active = self._sessions.get(self._active_session_id)
                if active and active.status in ACTIVE_STATUSES:
                    raise QrSessionConflictError(active.session_id)
                self._active_session_id = None

            session_id = uuid.uuid4().hex
            session = _QrSession(
                session_id=session_id,
                account_id=account_id,
                status='starting',
                created_at=now,
                expires_at=now + timedelta(seconds=self.session_timeout_seconds),
            )
            self._sessions[session_id] = session
            self._active_session_id = session_id
            session.task = asyncio.create_task(
                self._run_session(session),
                name=f'douyin-qr-login-{session_id}',
            )

        logger.info(
            '扫码登录会话已创建 session_id={} account_id={}',
            session_id,
            account_id,
        )
        try:
            await asyncio.wait_for(
                session.ready_event.wait(),
                timeout=min(self.qr_ready_timeout_seconds, self.session_timeout_seconds),
            )
        except asyncio.TimeoutError as error:
            await self._set_terminal(
                session,
                'failed',
                'QR_CODE_NOT_READY',
                '登录二维码生成超时',
            )
            if session.task and not session.task.done():
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
            raise QrLoginServiceError(
                'QR_CODE_NOT_READY',
                '登录二维码生成超时',
                502,
                session_id,
            ) from error

        async with self._lock:
            result = self._public_session(session, include_qrcode=True)
            # 创建响应取出后即释放图片，查询接口不重复传输
            session.qrcode_bytes = None
            status = session.status
            error_code = session.error_code
            error_message = session.error_message

        if status in ('failed', 'expired'):
            status_code = 503 if error_code == 'BROWSER_UNAVAILABLE' else 502
            raise QrLoginServiceError(
                error_code or 'QR_LOGIN_FAILED',
                error_message or '扫码登录失败',
                status_code,
                session_id,
            )
        return result

    async def get_session(self, session_id: str) -> dict:
        """查询会话状态，不返回二维码或凭证。"""
        async with self._lock:
            self._purge_expired_sessions_locked(self._now())
            session = self._sessions.get(session_id)
            if session is None:
                raise QrSessionNotFoundError(session_id)
            return self._public_session(session)

    async def request_sms_code(self, session_id: str) -> dict:
        """通知当前浏览器页面选择短信校验并请求验证码。"""
        async with self._lock:
            self._purge_expired_sessions_locked(self._now())
            session = self._sessions.get(session_id)
            if session is None:
                raise QrSessionNotFoundError(session_id)
            if session.status not in {'verification_required', 'waiting_sms_code'}:
                raise QrLoginServiceError(
                    'QR_SMS_NOT_AVAILABLE',
                    '当前扫码会话不允许请求短信验证码',
                    409,
                    session_id,
                )
            previous_status = session.status
            session.status = 'requesting_sms'
            session.error_code = None
            session.error_message = None
            action = (
                'resend_sms'
                if previous_status == 'waiting_sms_code'
                else 'request_sms'
            )
            session.verification_commands.put_nowait({'action': action})
            return self._public_session(session)

    async def submit_sms_code(self, session_id: str, code: str) -> dict:
        """将短信验证码安全传给当前浏览器页面。"""
        if not isinstance(code, str) or not re.fullmatch(r'\d{4,8}', code):
            raise QrLoginServiceError(
                'INVALID_SMS_CODE',
                '短信验证码必须是 4～8 位数字',
                422,
                session_id,
            )
        async with self._lock:
            self._purge_expired_sessions_locked(self._now())
            session = self._sessions.get(session_id)
            if session is None:
                raise QrSessionNotFoundError(session_id)
            if session.status != 'waiting_sms_code':
                raise QrLoginServiceError(
                    'QR_SMS_CODE_NOT_EXPECTED',
                    '当前扫码会话不在等待短信验证码',
                    409,
                    session_id,
                )
            session.status = 'verifying_sms'
            session.error_code = None
            session.error_message = None
            # 验证码只存在于内存队列，不写日志和持久化存储。
            session.verification_commands.put_nowait({
                'action': 'submit_sms',
                'code': code,
            })
            return self._public_session(session)

    async def cancel_session(self, session_id: str) -> dict:
        """取消会话并等待 Playwright 浏览器清理完毕。"""
        async with self._lock:
            self._purge_expired_sessions_locked(self._now())
            session = self._sessions.get(session_id)
            if session is None:
                raise QrSessionNotFoundError(session_id)
            task = session.task
            if session.persistence_started and session.status in ACTIVE_STATUSES:
                raise QrLoginServiceError(
                    'QR_SESSION_COMMITTING',
                    '扫码凭证正在提交，当前不可取消',
                    409,
                    session_id,
                )
            if session.status in ACTIVE_STATUSES and task and not task.done():
                task.cancel()

        if task and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            return self._public_session(session)

    async def shutdown(self) -> None:
        """停止服务并清理所有未结束的浏览器任务。"""
        async with self._lock:
            self._closed = True
            tasks = [
                session.task
                for session in self._sessions.values()
                if session.task is not None and not session.task.done()
            ]
            for session in self._sessions.values():
                task = session.task
                if task is not None and not task.done() and not session.persistence_started:
                    task.cancel()
        if tasks:
            # 持久化阶段不可取消，等待 DB 与内存账号池状态保持一致
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info('扫码登录服务已停止')

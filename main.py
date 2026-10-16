# coding=utf-8
import asyncio
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

from app.account_pool import AccountPool, NoAvailableAccountError
from app.account_store import CredentialStoreError, MySQLCredentialStore
from app.api_docs import (
    OPENAPI_TAGS, SWAGGER_UI_PARAMETERS, DOUYIN_LOGIN_TAG, DOUYIN_DATA_TAG, SYSTEM_TAG,
    append_parameter_docs, DOUYIN_QR_SESSION_DESCRIPTION,
)
from app.api_schemas import (
    QrSmsCodeRequest,
    QrSessionRequest,
    SearchWorksRequest,
    UserWorksRequest,
    VideoSubCommentsRequest,
    WorkCommentsRequest,
    WorksRequest,
)
from app.env_config import get_app_env, load_environment
from app.qr_login_service import QrLoginService, QrLoginServiceError
from app.spider_service import (
    AccountPinningDisabledError,
    SpiderService,
    UpstreamServiceError,
)
from dy_apis.douyin_api import DouyinRiskControlError
from app.xhs_router import create_xhs_router
from app.xhs_service import create_xhs_service
from app.xhs_login_service import XhsLoginService
from xhs_apis.errors import XhsApiError


PROD_LOG_DIR = '/data/logs/douyin-spider'


class _LoguruHandler(logging.Handler):
    """将 Uvicorn 标准日志转发到 Loguru。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(exception=record.exc_info).log(level, record.getMessage())


def configure_logging(app_env: str) -> None:
    """配置控制台日志，并在生产环境额外写入轮转日志文件。"""
    logger.remove()
    logger.add(sys.stdout, backtrace=False, diagnose=False)
    if app_env == 'prod':
        log_dir = os.getenv('LOG_DIR', PROD_LOG_DIR).strip()
        if not log_dir:
            raise RuntimeError('LOG_DIR 不能为空')
        os.makedirs(log_dir, exist_ok=True)
        logger.add(
            os.path.join(log_dir, 'douyin-spider-{time:YYYY-MM-DD}.log'),
            rotation='00:00',
            retention='30 days',
            encoding='utf-8',
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )

    # Uvicorn 启动与访问日志使用相同的控制台和文件输出。
    handler = _LoguruHandler()
    for logger_name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        standard_logger = logging.getLogger(logger_name)
        standard_logger.handlers = [handler]
        standard_logger.propagate = False


# Docker 显式设置 prod 时从导入阶段启用文件日志，否则先启用控制台日志。
configure_logging('prod' if os.getenv('APP_ENV') == 'prod' else 'dev')


# 常见错误响应仅用于完善 OpenAPI 文档，不改变实际异常处理。
SCRAPE_ERROR_RESPONSES = {
    422: {'description': '请求参数或抖音链接校验失败。'},
    429: {'description': '抖音上游返回明确的访问频率或安全验证信号。'},
    502: {'description': '抖音上游网络异常、响应异常或全部作品抓取失败。'},
    503: {'description': '当前没有可用账号，或账号正在冷却。'},
}

QR_ERROR_RESPONSES = {
    404: {'description': '扫码登录会话不存在。'},
    409: {'description': '账号已有进行中的扫码会话，或会话状态不允许当前操作。'},
    422: {'description': '请求参数校验失败。'},
    503: {'description': '扫码登录服务不可用。'},
}


def _positive_int_from_env(name: str, default: int) -> int:
    """读取并校验正整数配置。"""
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f'{name} 必须是正整数') from error
    if value <= 0:
        raise RuntimeError(f'{name} 必须是正整数')
    return value


def _response(request: Request, data: dict | list, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            'success': status_code < 400,
            'request_id': request.state.request_id,
            'data': data,
        },
    )


def _error_response(request: Request, code: str, message: str, status_code: int,
                    details=None) -> JSONResponse:
    error = {'code': code, 'message': message}
    if details is not None:
        error['details'] = details
    return JSONResponse(
        status_code=status_code,
        content={
            'success': False,
            'request_id': getattr(request.state, 'request_id', ''),
            'error': error,
        },
    )


async def _refresh_account_pool_periodically(
        store,
        account_pool: AccountPool,
        interval_seconds: float,
        platform: str = 'douyin',
) -> None:
    """定时从 MySQL 合并最新账号；失败时保留现有内存账号。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            load_task = asyncio.create_task(asyncio.to_thread(store.load_latest))
            try:
                records = await asyncio.shield(load_task)
            except asyncio.CancelledError:
                # 等待已进入线程的查询退出，避免关闭连接池时仍有查询运行。
                await asyncio.gather(load_task, return_exceptions=True)
                raise
            changed = account_pool.refresh(records)
            logger.info(
                'MySQL 账号池定时刷新完成 loaded={} changed={} interval_seconds={} platform={}',
                len(records),
                changed,
                interval_seconds,
                platform,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # 不记录异常正文，避免数据库连接信息进入日志。
            logger.error('MySQL 账号池定时刷新失败 error_type={}', error.__class__.__name__)


def create_app(
        service: SpiderService | None = None,
        qr_login_service: QrLoginService | None = None,
        credential_store: MySQLCredentialStore | None = None,
        xhs_service=None,
        xhs_login_service=None,
) -> FastAPI:
    """创建应用；测试可注入不访问真实网络和数据库的服务。"""

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        load_environment()
        app_env = get_app_env()
        configure_logging(app_env)
        app_instance.state.app_env = app_env
        store = getattr(app_instance.state, 'credential_store', None)
        spider = getattr(app_instance.state, 'spider_service', None)
        qr_service = getattr(app_instance.state, 'qr_login_service', None)
        owns_store = False
        refresh_task = None
        xhs_refresh_task = None
        xhs = getattr(app_instance.state, 'xhs_service', None)
        xhs_login = getattr(app_instance.state, 'xhs_login_service', None)
        try:
            if spider is None:
                if store is None:
                    store = MySQLCredentialStore.from_env()
                    owns_store = True
                # 数据库是 API 账号池的核心依赖，连接失败时阻止启动
                await asyncio.to_thread(store.check_connection)
                account_pool = await asyncio.to_thread(AccountPool.from_store, store)
                spider = SpiderService(
                    account_pool=account_pool,
                    max_concurrent=_positive_int_from_env('MAX_CONCURRENT_REQUESTS', 2),
                )
                app_instance.state.credential_store = store
                app_instance.state.spider_service = spider

            if qr_service is None and store is not None:
                qr_service = QrLoginService(store, spider.account_pool)
                app_instance.state.qr_login_service = qr_service

            # 注入测试服务时不自动创建真实数据库和上游客户端。
            enabled = os.getenv('XHS_ENABLED', 'true').strip().lower()
            if enabled not in {'true', 'false', '1', '0'}:
                raise RuntimeError('XHS_ENABLED 必须是 true/false 或 1/0')
            if xhs is None and service is None and enabled in {'true', '1'} and isinstance(store, MySQLCredentialStore):
                xhs = await asyncio.to_thread(create_xhs_service, store)
                app_instance.state.xhs_service = xhs
            if xhs is not None:
                if xhs_login is None and xhs.credential_store is not None:
                    xhs_login = XhsLoginService(xhs.credential_store, xhs.account_pool)
                    app_instance.state.xhs_login_service = xhs_login
                if xhs.credential_store is not None:
                    xhs_refresh_task = asyncio.create_task(
                        _refresh_account_pool_periodically(
                            xhs.credential_store, xhs.account_pool,
                            _positive_int_from_env('ACCOUNT_REFRESH_INTERVAL_SECONDS', 300), platform='xhs',
                        ), name='xhs-account-pool-refresh',
                    )

            # 账号定向只允许 dev 测试实例，生产环境即使误配也强制关闭。
            spider.test_account_pinning_enabled = bool(
                spider.test_account_pinning_enabled and app_env == 'dev'
            )

            refresh_method = getattr(spider.account_pool, 'refresh', None)
            if store is not None and callable(refresh_method):
                refresh_interval = _positive_int_from_env(
                    'ACCOUNT_REFRESH_INTERVAL_SECONDS', 300
                )
                refresh_task = asyncio.create_task(
                    _refresh_account_pool_periodically(
                        store,
                        spider.account_pool,
                        refresh_interval,
                    ),
                    name='mysql-account-pool-refresh',
                )
                app_instance.state.account_refresh_interval_seconds = refresh_interval

            stats = spider.account_stats()
            logger.info(
                'DouYin Spider API 启动，环境={}，最大并发={}，账号总数={}，可用账号={}',
                app_env,
                spider.max_concurrent,
                stats['total'],
                stats['available'],
            )
            yield
        finally:
            # 任一清理失败也继续关闭其他服务，最后再释放共享数据库。
            for task in (xhs_refresh_task, refresh_task):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            for instance in (xhs_login, qr_service):
                if instance is not None:
                    try:
                        await instance.shutdown()
                    except Exception as error:
                        logger.error('登录服务关闭失败 error_type={}', type(error).__name__)
            for instance in (xhs, store if owns_store else None):
                if instance is not None:
                    try:
                        await asyncio.to_thread(instance.close)
                    except Exception as error:
                        logger.error('服务资源关闭失败 error_type={}', type(error).__name__)
            logger.info('DouYin Spider API 停止')

    application = FastAPI(
        title='抖音 / 小红书 API',
        version='1.7.0',
        description=(
            '公司内部使用的抖音、小红书多账号数据抓取 API。\n\n'
            '**开始使用**：展开对应平台的“登录与账号”，完成登录后使用“数据查询”。\n\n'
            '小红书：短信或扫码两步登录；抖音：创建扫码会话 → 扫码 → 查询状态。\n\n'
            '所有接口统一返回 JSON：成功响应包含 `success`、`request_id` 和 `data`；'
            '失败响应包含 `success`、`request_id` 和 `error`。'
        ),
        openapi_tags=OPENAPI_TAGS,
        swagger_ui_parameters=SWAGGER_UI_PARAMETERS,
        lifespan=lifespan,
        docs_url='/api/docs',
        redoc_url='/api/redoc',
        openapi_url='/api/openapi.json',
    )
    if service is not None:
        application.state.spider_service = service
    if qr_login_service is not None:
        application.state.qr_login_service = qr_login_service
    if credential_store is not None:
        application.state.credential_store = credential_store
    if xhs_service is not None:
        application.state.xhs_service = xhs_service
    if xhs_login_service is not None:
        application.state.xhs_login_service = xhs_login_service
    application.include_router(create_xhs_router(_response))

    @application.exception_handler(XhsApiError)
    async def xhs_error_handler(request: Request, error: XhsApiError):
        return _error_response(request, error.code, error.message, error.status_code, error.details)

    @application.middleware('http')
    async def request_context(request: Request, call_next):
        # 服务端生成不可控的追踪 ID，避免外部请求头污染日志。
        request.state.request_id = uuid.uuid4().hex
        started_at = time.perf_counter()
        logger.info('[{}] 请求开始 method={} path={}', request.state.request_id, request.method, request.url.path)
        response = await call_next(request)
        response.headers['X-Request-ID'] = request.state.request_id
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            '[{}] 请求结束 method={} path={} status={} elapsed_ms={:.2f}',
            request.state.request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, error: RequestValidationError):
        # Pydantic 错误上下文可能包含不可序列化异常，只返回安全字段
        details = [
            {'loc': list(item['loc']), 'message': item['msg'], 'type': item['type']}
            for item in error.errors()
        ]
        return _error_response(request, 'INVALID_REQUEST', '请求参数校验失败', 422, details)

    @application.exception_handler(NoAvailableAccountError)
    async def no_account_handler(request: Request, error: NoAvailableAccountError):
        details = None
        if error.retry_after_seconds is not None:
            details = {'retry_after_seconds': error.retry_after_seconds}
        return _error_response(request, 'NO_AVAILABLE_ACCOUNT', '当前没有可用的抖音账号', 503, details)

    @application.exception_handler(QrLoginServiceError)
    async def qr_login_error_handler(request: Request, error: QrLoginServiceError):
        details = {'session_id': error.session_id} if error.session_id else None
        return _error_response(request, error.code, error.message, error.status_code, details)

    @application.exception_handler(UpstreamServiceError)
    async def upstream_error_handler(request: Request, error: UpstreamServiceError):
        return _error_response(request, 'UPSTREAM_ERROR', error.message, 502, error.details)

    @application.exception_handler(AccountPinningDisabledError)
    async def account_pinning_disabled_handler(
            request: Request,
            error: AccountPinningDisabledError,
    ):
        return _error_response(
            request,
            'ACCOUNT_PINNING_DISABLED',
            '服务未启用测试账号定向能力',
            403,
        )

    @application.exception_handler(DouyinRiskControlError)
    async def risk_control_error_handler(request: Request, error: DouyinRiskControlError):
        # 仅返回稳定信号，不暴露上游正文和请求参数。
        return _error_response(
            request,
            'UPSTREAM_RISK_CONTROL',
            '抖音上游触发访问限制或安全验证',
            429,
            {'signal': error.signal},
        )

    @application.exception_handler(Exception)
    async def internal_error_handler(request: Request, error: Exception):
        # 不记录异常正文，避免意外泄漏凭证或数据库连接信息
        logger.error('[{}] 未处理异常 type={}', request.state.request_id, error.__class__.__name__)
        return _error_response(request, 'INTERNAL_ERROR', '服务内部错误', 500)

    @application.get(
        '/api/health',
        tags=[SYSTEM_TAG],
        summary='检查服务健康状态',
        description='检查运行环境、数据库连接、账号池状态以及服务最大并发数。',
        response_description='服务及其依赖的当前状态。',
        responses={503: {'description': '数据库连接不可用。'}},
    )
    def health(request: Request):
        spider = request.app.state.spider_service
        store = getattr(request.app.state, 'credential_store', None)
        database_status = 'not_configured'
        status_code = 200
        if store is not None:
            try:
                store.check_connection()
                database_status = 'ok'
            except CredentialStoreError:
                database_status = 'unavailable'
                status_code = 503

        stats = spider.account_stats()
        xhs_instance = getattr(request.app.state, 'xhs_service', None)
        xhs_stats = xhs_instance.account_pool.stats() if xhs_instance is not None else None
        available = stats['available'] > 0 or (xhs_stats is not None and xhs_stats['available'] > 0)
        service_status = 'ok' if available else 'not_authenticated'
        if database_status == 'unavailable':
            service_status = 'database_unavailable'
        return _response(request, {
            'status': service_status,
            'environment': request.app.state.app_env,
            'database': database_status,
            'accounts': stats,
            'max_concurrent_requests': spider.max_concurrent,
            'max_concurrent_requests_per_account': spider.max_concurrent_per_account,
            'test_account_pinning_enabled': spider.test_account_pinning_enabled,
            'xhs': {
                'enabled': xhs_instance is not None,
                'accounts': xhs_stats,
            },
        }, status_code=status_code)

    @application.get(
        '/api/v1/douyin/auth/accounts',
        tags=[DOUYIN_LOGIN_TAG],
        summary='查询抖音账号池',
        description=(
            '返回账号池中的账号标识、凭证版本、更新时间和运行状态。'
            '出于安全考虑，不返回 Cookie、Ticket、证书或私钥。'
        ),
        response_description='不包含敏感凭证的账号列表。',
    )
    def list_accounts(request: Request):
        return _response(request, {'items': request.app.state.spider_service.list_accounts()})

    @application.post(
        '/api/v1/douyin/auth/qr-sessions',
        tags=[DOUYIN_LOGIN_TAG],
        status_code=201,
        summary='创建扫码登录会话',
        description=(
            '为指定账号启动扫码登录。创建响应是唯一包含 `qrcode_data_url` 的响应，'
            '客户端应立即展示该 Base64 PNG Data URL，并通过状态接口轮询结果。'
        ),
        response_description='已创建的扫码登录会话和二维码。',
        responses=QR_ERROR_RESPONSES,
    )
    async def create_qr_session(payload: QrSessionRequest, request: Request):
        qr_service = getattr(request.app.state, 'qr_login_service', None)
        if qr_service is None:
            return _error_response(request, 'QR_LOGIN_UNAVAILABLE', '扫码登录服务不可用', 503)
        data = await qr_service.create_session(payload.account_id)
        return _response(request, data, status_code=201)

    @application.get(
        '/api/v1/douyin/auth/qr-sessions/{session_id}',
        tags=[DOUYIN_LOGIN_TAG],
        summary='查询扫码登录状态',
        description=(
            '查询扫码会话的当前状态。扫码后如需短信验证，会依次出现 '
            '`verification_required`、`requesting_sms`、`waiting_sms_code` 和 '
            '`verifying_sms`；其余状态包括 `starting`、`waiting_scan`、`committing`、'
            '`succeeded`、`expired`、`failed` 和 `cancelled`。'
        ),
        response_description='扫码登录会话的当前状态。',
        responses=QR_ERROR_RESPONSES,
    )
    async def get_qr_session(
            session_id: Annotated[str, Path(description=DOUYIN_QR_SESSION_DESCRIPTION)],
            request: Request,
    ):
        qr_service = getattr(request.app.state, 'qr_login_service', None)
        if qr_service is None:
            return _error_response(request, 'QR_LOGIN_UNAVAILABLE', '扫码登录服务不可用', 503)
        return _response(request, await qr_service.get_session(session_id))

    @application.post(
        '/api/v1/douyin/auth/qr-sessions/{session_id}/sms/request',
        tags=[DOUYIN_LOGIN_TAG],
        summary='请求扫码登录短信验证码',
        description=(
            '扫码会话状态为 `verification_required` 时选择“接收短信验证码”；状态为 '
            '`waiting_sms_code` 时再次调用则重新发送。随后轮询到 `waiting_sms_code` 即可提交验证码。'
        ),
        response_description='已接收短信请求的扫码登录会话。',
        responses=QR_ERROR_RESPONSES,
    )
    async def request_qr_sms_code(
            session_id: Annotated[str, Path(description=DOUYIN_QR_SESSION_DESCRIPTION)],
            request: Request,
    ):
        qr_service = getattr(request.app.state, 'qr_login_service', None)
        if qr_service is None:
            return _error_response(request, 'QR_LOGIN_UNAVAILABLE', '扫码登录服务不可用', 503)
        return _response(request, await qr_service.request_sms_code(session_id))

    @application.post(
        '/api/v1/douyin/auth/qr-sessions/{session_id}/sms/verify',
        tags=[DOUYIN_LOGIN_TAG],
        summary='提交扫码登录短信验证码',
        description=(
            '仅当扫码会话状态为 `waiting_sms_code` 时调用。验证码只在当前登录任务的内存中短暂传递，'
            '不会写入应用日志或数据库；提交后继续轮询会话直到 `succeeded`。'
        ),
        response_description='正在校验短信验证码的扫码登录会话。',
        responses=QR_ERROR_RESPONSES,
    )
    async def verify_qr_sms_code(
            session_id: Annotated[str, Path(description=DOUYIN_QR_SESSION_DESCRIPTION)],
            payload: QrSmsCodeRequest,
            request: Request,
    ):
        qr_service = getattr(request.app.state, 'qr_login_service', None)
        if qr_service is None:
            return _error_response(request, 'QR_LOGIN_UNAVAILABLE', '扫码登录服务不可用', 503)
        return _response(
            request,
            await qr_service.submit_sms_code(session_id, payload.code),
        )

    @application.delete(
        '/api/v1/douyin/auth/qr-sessions/{session_id}',
        tags=[DOUYIN_LOGIN_TAG],
        summary='取消扫码登录会话',
        description='取消尚未结束的扫码登录会话，并关闭其浏览器资源。',
        response_description='取消后的扫码登录会话状态。',
        responses=QR_ERROR_RESPONSES,
    )
    async def cancel_qr_session(
            session_id: Annotated[str, Path(description=DOUYIN_QR_SESSION_DESCRIPTION)],
            request: Request,
    ):
        qr_service = getattr(request.app.state, 'qr_login_service', None)
        if qr_service is None:
            return _error_response(request, 'QR_LOGIN_UNAVAILABLE', '扫码登录服务不可用', 503)
        return _response(request, await qr_service.cancel_session(session_id))

    @application.post(
        '/api/v1/douyin/video_info',
        tags=[DOUYIN_DATA_TAG],
        summary='批量获取作品详情',
        description=(
            '根据单个 `video_id` 或 1～20 个抖音作品链接获取标准化作品详情。批量请求允许部分成功：'
            '成功项目位于 `data.items`，失败项目位于 `data.errors`；全部失败时返回 HTTP 502。'
        ),
        response_description='作品详情、失败项目和实际使用的账号信息。',
        responses=SCRAPE_ERROR_RESPONSES,
    )
    def get_works(payload: WorksRequest, request: Request):
        data = request.app.state.spider_service.get_works(payload.work_urls, request.state.request_id)
        return _response(request, data)

    @application.post(
        '/api/v1/douyin/video_comments',
        tags=[DOUYIN_DATA_TAG],
        summary='获取视频一级评论',
        description=(
            '通过 `url` 或 `video_id` 分页获取指定作品的一级评论，不自动抓取回复。首次请求使用 `cursor=0`，'
            '后续请求使用响应中的 `data.next_cursor`；`data.has_more` 表示是否还有下一页。'
        ),
        response_description='评论列表、分页信息和实际使用的账号信息。',
        responses=SCRAPE_ERROR_RESPONSES,
    )
    def get_work_comments(payload: WorkCommentsRequest, request: Request):
        data = request.app.state.spider_service.get_work_comments(
            payload.work_url,
            payload.cursor,
            payload.count,
            request.state.request_id,
        )
        return _response(request, data)

    @application.post(
        '/api/v1/douyin/video_sub_comments',
        tags=[DOUYIN_DATA_TAG],
        summary='获取视频二级评论',
        description=(
            '根据 `video_id` 和一级评论 `comment_id` 分页获取回复。首次请求使用 `cursor=0`，'
            '后续请求使用响应中的 `data.next_cursor`；`data.has_more` 表示是否还有下一页。'
        ),
        response_description='二级评论列表、分页信息和实际使用的账号信息。',
        responses=SCRAPE_ERROR_RESPONSES,
    )
    def get_video_sub_comments(payload: VideoSubCommentsRequest, request: Request):
        data = request.app.state.spider_service.get_video_sub_comments(
            payload.video_id,
            payload.comment_id,
            payload.cursor,
            payload.count,
            request.state.request_id,
        )
        return _response(request, data)

    @application.post(
        '/api/v1/douyin/user_videos',
        tags=[DOUYIN_DATA_TAG],
        summary='获取用户作品',
        description='`user_id` 自动识别用户 ID 或完整 HTTPS 抖音主页链接，也兼容旧参数 `user_url`；获取指定页数的标准化作品数据。',
        response_description='用户信息、作品列表和实际使用的账号信息。',
        responses=SCRAPE_ERROR_RESPONSES,
    )
    def get_user_works(payload: UserWorksRequest, request: Request):
        data = request.app.state.spider_service.get_user_works(
            payload.resolved_user_url,
            payload.page_num,
            request.state.request_id,
        )
        return _response(request, data)

    @application.post(
        '/api/v1/douyin/search_videos',
        tags=[DOUYIN_DATA_TAG],
        summary='搜索抖音作品',
        description='按照关键词、排序方式、发布时间、视频时长、搜索范围和内容类型搜索作品。',
        response_description='搜索条件、作品列表和实际使用的账号信息。',
        responses=SCRAPE_ERROR_RESPONSES,
    )
    def search_works(payload: SearchWorksRequest, request: Request):
        data = request.app.state.spider_service.search_works(payload, request.state.request_id)
        return _response(request, data)

    append_parameter_docs(application)
    return application


app = create_app()

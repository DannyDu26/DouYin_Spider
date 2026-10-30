"""小红书路由，复用应用统一响应封装。"""
from fastapi import APIRouter, Depends, Header, Request

from app.browser_cookie_import import require_import_token

from app.api_docs import XHS_DATA_TAG, XHS_LOGIN_TAG, sms_response_docs, append_parameter_docs
from app.xhs_schemas import (
    NoteRequest, SearchNotesRequest, UserRequest, UserNotesRequest,
    NoteCommentsRequest, NoteSubCommentsRequest, SmsSessionRequest, SmsLoginRequest,
    QrSessionRequest, QrLoginRequest, CookieImportRequest,
)
from xhs_apis.errors import XhsApiError


def require_cookie_import_token(x_xhs_import_token: str = Header(default='', description='浏览器 Cookie 导入的独立同步密钥')):
    """导入凭证使用独立密钥，未配置时关闭入口。"""
    require_import_token(x_xhs_import_token, 'XHS', XhsApiError)


def create_xhs_router(response):
    # 每个接口只指定一个分组，避免父子 tags 叠加造成重复展示。
    router = APIRouter(prefix='/api/v1/xhs', responses={
        401: {'description': '小红书账号登录已过期，请重新登录（XHS_AUTH_EXPIRED）。'},
        422: {'description': '参数、链接或验证码校验失败。'},
        429: {'description': '发送过于频繁或小红书上游要求安全验证。'},
        502: {'description': '小红书上游请求或响应异常。'},
        503: {'description': '服务未启用、账号不可用、排队超时或上游暂时不可用；按 error.code、details.reason 和 Retry-After 处理。'},
    })

    def service(request, name='xhs_service'):
        instance = getattr(request.app.state, name, None)
        if instance is None:
            raise XhsApiError('XHS_SERVICE_UNAVAILABLE', '小红书服务未启用', 503)
        return instance

    @router.post('/note_info', tags=[XHS_DATA_TAG], summary='笔记详情',
                 description='传完整笔记 url，或 note_id + xsec_token。返回 data.note，保留原始笔记结构。')
    def note_info(payload: NoteRequest, request: Request):
        return response(request, service(request).note_info(payload, request.state.request_id))

    @router.post('/search_notes', tags=[XHS_DATA_TAG], summary='搜索笔记',
                 description='首次 page=1；翻页续传 search_id、search_session_id，并保持搜索条件一致。')
    def search_notes(payload: SearchNotesRequest, request: Request):
        return response(request, service(request).search_notes(payload, request.state.request_id))

    @router.post('/user_info', tags=[XHS_DATA_TAG], summary='用户信息',
                 description='传 user_id 或完整 user_url。返回 data.user，保留上游用户结构。')
    def user_info(payload: UserRequest, request: Request):
        return response(request, service(request).user_info(payload, request.state.request_id))

    @router.post('/user_notes', tags=[XHS_DATA_TAG], summary='用户笔记',
                 description='首次 cursor 为空；后续原样传回 data.next_cursor，data.has_more 表示是否还有下一页。')
    def user_notes(payload: UserNotesRequest, request: Request):
        return response(request, service(request).user_notes(payload, request.state.request_id))

    @router.post('/note_comments', tags=[XHS_DATA_TAG], summary='一级评论',
                 description='传笔记链接或 ID 与 token。首次 cursor 为空，翻页续传 data.next_cursor，不自动获取回复。')
    def note_comments(payload: NoteCommentsRequest, request: Request):
        return response(request, service(request).note_comments(payload, request.state.request_id))

    @router.post('/note_sub_comments', tags=[XHS_DATA_TAG], summary='评论回复',
                 description='root_comment_id 为一级评论 ID，初始 cursor 可取该评论的 sub_comment_cursor；翻页续传 data.next_cursor。')
    def note_sub_comments(payload: NoteSubCommentsRequest, request: Request):
        return response(request, service(request).note_sub_comments(payload, request.state.request_id))

    @router.post('/auth/sms/send', tags=[XHS_LOGIN_TAG], summary='1. 发送验证码',
                 responses=sms_response_docs('waiting_sms_code'),
                 description='自动初始化并发送短信，等待结果后返回。保存 data.session_id，供下一步登录使用；相同账号、手机号和区号可重发，默认间隔 60 秒。')
    async def send_sms(payload: SmsSessionRequest, request: Request):
        return response(request, await service(request, 'xhs_login_service').send_sms(payload))

    @router.post('/auth/sms/login', tags=[XHS_LOGIN_TAG], summary='2. 提交验证码并登录',
                 responses=sms_response_docs('succeeded'),
                 description='使用上一步的 session_id 和短信 code。返回 data.status=succeeded 时，凭证已保存且账号已入池，可直接调用数据接口。')
    async def login_sms(payload: SmsLoginRequest, request: Request):
        return response(request, await service(request, 'xhs_login_service').login_sms(payload.session_id, payload.code))

    @router.post('/auth/qr/create', tags=[XHS_LOGIN_TAG], summary='扫码 1. 生成二维码',
                 responses=sms_response_docs('waiting_scan', kind='qr', include_qrcode=True),
                 description='自动初始化设备并返回二维码 PNG Data URL 和 session_id。展示 data.qrcode_data_url，使用小红书 App 扫码并确认，然后调用扫码登录接口。')
    async def create_qrcode(payload: QrSessionRequest, request: Request):
        return response(request, await service(request, 'xhs_login_service').create_qrcode(payload.account_id))

    @router.post('/auth/qr/login', tags=[XHS_LOGIN_TAG], summary='扫码 2. 等待确认并登录',
                 responses=sms_response_docs('succeeded', kind='qr'),
                 description='提交二维码接口返回的 session_id。服务端等待扫码和手机确认，验证正式用户并保存凭证、加入账号池后返回 succeeded；无需轮询，客户端需设置足够的超时。')
    async def login_qrcode(payload: QrLoginRequest, request: Request):
        return response(request, await service(request, 'xhs_login_service').login_qrcode(payload.session_id))

    @router.post('/auth/cookie/import', tags=[XHS_LOGIN_TAG], summary='导入浏览器登录 Cookie',
                 dependencies=[Depends(require_cookie_import_token)],
                 responses={401: {'description': '同步密钥无效，或小红书会话已过期。'}},
                 description='扩展手动上传 Cookie；请求头 X-XHS-Import-Token 必须匹配服务端 XHS_COOKIE_IMPORT_TOKEN。验证正式用户后保存并加入账号池，重复 account_id 更新已有凭证；未配置密钥时禁用。响应不包含 Cookie。')
    async def import_cookie(payload: CookieImportRequest, request: Request):
        return response(request, await service(request, 'xhs_login_service').import_cookie(payload))

    @router.get('/auth/accounts', tags=[XHS_LOGIN_TAG], summary='查看账号池')
    def accounts(request: Request):
        return response(request, {'items': service(request).account_pool.list_accounts()})

    # 在挂载前补充子路由文档，兼容 FastAPI 延迟展开路由的版本。
    append_parameter_docs(router)
    return router

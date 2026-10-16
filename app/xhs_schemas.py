"""小红书 API 请求校验，统一解析完整链接与 ID。"""
import re
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.account_store import validate_account_id


XHS_ID_PATTERN = r'^[0-9a-fA-F]{24}$'


def parse_xhs_url(value: str, kind: str) -> tuple[str, dict]:
    """只解析完整 HTTPS 链接，拒绝短链和含糊定位参数。"""
    parsed = urlparse(value)
    if len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise ValueError('小红书链接无效')
    if parsed.scheme != 'https' or parsed.hostname not in {'xiaohongshu.com', 'www.xiaohongshu.com'}:
        raise ValueError('必须提供完整 HTTPS 小红书链接')
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('小红书链接不能包含认证信息或非标准端口')
    paths = {'note': r'/(?:explore|discovery/item)/([0-9a-fA-F]{24})/?',
             'user': r'/user/profile/([0-9a-fA-F]{24})/?'}
    match = re.fullmatch(paths[kind], parsed.path)
    if not match:
        raise ValueError('链接中缺少有效的小红书 ID')
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key in ('xsec_token', 'xsec_source'):
        if len(query.get(key, [])) > 1:
            raise ValueError(f'链接中 {key} 不能重复')
    return match.group(1), {key: values[0] for key, values in query.items()}


class XhsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class NoteRequest(XhsRequest):
    model_config = ConfigDict(json_schema_extra={'examples': [
        {'note_id': 'a' * 24, 'xsec_token': '替换为笔记链接或搜索结果中的token'},
    ]})
    url: str | None = Field(default=None, max_length=4096,
                            description='完整 HTTPS 小红书笔记链接，路径为 /explore/{id} 或 /discovery/item/{id}；与 note_id 必须且只能提供一个。不支持短链；自动提取链接中的 xsec_token 和 xsec_source。',
                            examples=['https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa?xsec_token=替换为真实token&xsec_source=pc_search'])
    note_id: str | None = Field(default=None, pattern=XHS_ID_PATTERN,
                                description='笔记 ID，24 位十六进制字符串，可从笔记链接路径或搜索结果的 id 取得；与 url 二选一，使用 ID 时必须同时提供 xsec_token。',
                                examples=['a' * 24])
    xsec_token: str | None = Field(default=None, max_length=4096,
                                   description='笔记访问校验 token，必须有值；从完整笔记链接、搜索结果或用户笔记结果取得。链接已包含时可省略；显式传入时须与链接中相同。',
                                   examples=['替换为笔记链接或查询结果中的token'])
    xsec_source: str | None = Field(default=None, max_length=100,
                                    description='笔记访问来源，优先沿用链接中的值；未提供或为空时使用 pc_search。常见值为 pc_search、pc_user、pc_feed；显式传入时须与链接中相同。',
                                    examples=['pc_search'])

    @model_validator(mode='after')
    def resolve_note(self):
        if (self.url is None) == (self.note_id is None):
            raise ValueError('url 和 note_id 必须且只能提供一个')
        if self.url:
            note_id, query = parse_xhs_url(self.url, 'note')
            self.note_id = note_id
            for key in ('xsec_token', 'xsec_source'):
                current = getattr(self, key)
                embedded = query.get(key)
                if current is not None and embedded is not None and current != embedded:
                    raise ValueError(f'{key} 与链接中的值冲突')
                setattr(self, key, current if current is not None else embedded)
        if not self.xsec_token:
            raise ValueError('需要 xsec_token，可从完整链接、搜索或用户笔记结果取得')
        self.xsec_source = self.xsec_source or 'pc_search'
        return self

    @property
    def note_url(self):
        return f'https://www.xiaohongshu.com/explore/{self.note_id}?' + urlencode({
            'xsec_token': self.xsec_token, 'xsec_source': self.xsec_source,
        })


class UserRequest(XhsRequest):
    model_config = ConfigDict(json_schema_extra={'examples': [{'user_id': 'b' * 24}]})
    user_id: str | None = Field(default=None, pattern=XHS_ID_PATTERN,
                                description='小红书用户 ID，24 位十六进制字符串，从主页 /user/profile/{id} 或笔记作者信息取得；与 user_url 必须且只能提供一个。',
                                examples=['b' * 24])
    user_url: str | None = Field(default=None, max_length=4096,
                                 description='完整 HTTPS 小红书主页链接，路径为 /user/profile/{id}；与 user_id 二选一。不支持短链，查询用户笔记时自动提取链接中的 token/source。',
                                 examples=['https://www.xiaohongshu.com/user/profile/' + 'b' * 24])

    @model_validator(mode='after')
    def resolve_user(self):
        if (self.user_id is None) == (self.user_url is None):
            raise ValueError('user_id 和 user_url 必须且只能提供一个')
        if self.user_url:
            self.user_id, _ = parse_xhs_url(self.user_url, 'user')
        return self


class UserNotesRequest(UserRequest):
    model_config = ConfigDict(json_schema_extra={'examples': [{'user_id': 'b' * 24, 'cursor': ''}]})
    cursor: str = Field(default='', max_length=4096,
                        description='用户笔记分页游标；首次省略或传空字符串，后续原样传回上次响应的 data.next_cursor；data.has_more=false 时停止翻页。不是页码。', examples=[''])
    xsec_token: str | None = Field(default=None, max_length=4096,
                                   description='用户主页访问 token，可从 user_url 的查询参数取得；省略时自动提取，没有则使用空字符串。显式传入时须与链接中相同，是否必须由上游决定。',
                                   examples=['替换为主页链接中的token'])
    xsec_source: str | None = Field(default=None, max_length=100,
                                    description='用户主页访问来源，优先沿用 user_url 中的值；未提供或为空时使用 pc_search，显式传入时须与链接中相同。', examples=['pc_search'])

    @model_validator(mode='after')
    def resolve_tokens(self):
        query = parse_xhs_url(self.user_url, 'user')[1] if self.user_url else {}
        for key in ('xsec_token', 'xsec_source'):
            current = getattr(self, key)
            embedded = query.get(key)
            if current is not None and embedded is not None and current != embedded:
                raise ValueError(f'{key} 与链接中的值冲突')
            setattr(self, key, current if current is not None else embedded)
        self.xsec_token = self.xsec_token or ''
        self.xsec_source = self.xsec_source or 'pc_search'
        return self


class NoteCommentsRequest(NoteRequest):
    model_config = ConfigDict(json_schema_extra={'examples': [
        {'note_id': 'a' * 24, 'xsec_token': '替换为笔记token', 'cursor': ''},
    ]})
    cursor: str = Field(default='', max_length=4096,
                        description='一级评论分页游标；首次省略或传空字符串，后续原样传回 data.next_cursor，data.has_more=false 时停止；不自动获取评论回复。', examples=[''])


class NoteSubCommentsRequest(XhsRequest):
    model_config = ConfigDict(json_schema_extra={'examples': [
        {'note_id': 'a' * 24, 'root_comment_id': 'c' * 24, 'xsec_token': '替换为笔记token', 'cursor': ''},
    ]})
    note_id: str = Field(pattern=XHS_ID_PATTERN,
                         description='一级评论所属的笔记 ID，24 位十六进制字符串；必须与 root_comment_id 所属笔记一致。', examples=['a' * 24])
    root_comment_id: str = Field(pattern=XHS_ID_PATTERN,
                                 description='需要查询回复的一级评论 ID，24 位十六进制字符串，取一级评论响应 data.items 中该评论的 id。', examples=['c' * 24])
    xsec_token: str = Field(min_length=1, max_length=4096,
                            description='该笔记的非空访问 token，从笔记链接、搜索或用户笔记结果取得；不能使用其他笔记的 token。', examples=['替换为笔记token'])
    cursor: str = Field(default='', max_length=4096,
                        description='评论回复分页游标；首次可传一级评论条目的 sub_comment_cursor，也可省略或传空字符串；后续原样传回 data.next_cursor，data.has_more=false 时停止。', examples=[''])


class SearchNotesRequest(XhsRequest):
    model_config = ConfigDict(json_schema_extra={'examples': [{'keyword': '猫', 'page': 1}]})
    keyword: str = Field(min_length=1, max_length=200,
                         description='搜索关键词，去除首尾空白后为 1～200 个字符；同一次搜索翻页保持不变。', examples=['猫'])
    page: int = Field(default=1, ge=1, le=1000, strict=True,
                      description='搜索页码，整数 1～1000，首次为 1；后续使用 data.next_page。每页请求 20 条，实际数量由上游决定。page>1 时必须续传两个搜索 ID。', examples=[1])
    search_id: str | None = Field(default=None, min_length=1, max_length=200,
                                  description='同一次搜索的标识；首页省略时自动生成并在 data.search_id 返回，后续页必须原样续传，并保持关键词、排序和筛选一致。', examples=['替换为上一页data.search_id'])
    search_session_id: str | None = Field(default=None, min_length=1, max_length=200,
                                          description='搜索会话标识；首页省略时自动生成并在 data.search_session_id 返回，翻页必须与 search_id 一起原样续传。', examples=['替换为上一页data.search_session_id'])
    sort: Literal['general', 'time_descending', 'popularity_descending', 'comment_descending', 'collect_descending'] = Field(
        default='general', description='排序：general 综合；time_descending 最新发布；popularity_descending 最多点赞；comment_descending 最多评论；collect_descending 最多收藏。', examples=['general'])
    note_type: int = Field(default=0, ge=0, le=2, strict=True,
                           description='笔记类型：0 不限；1 视频笔记；2 图文笔记。使用 JSON 整数。', examples=[0])
    note_time: int = Field(default=0, ge=0, le=3, strict=True,
                           description='发布时间：0 不限；1 一天内；2 一周内；3 半年内。使用 JSON 整数。', examples=[0])
    note_range: int = Field(default=0, ge=0, le=3, strict=True,
                            description='搜索范围：0 不限；1 已看过；2 未看过；3 已关注。使用 JSON 整数，筛选结果与实际调度的登录账号有关。', examples=[0])
    pos_distance: int = Field(default=0, ge=0, le=2, strict=True,
                              description='位置范围：0 不限；1 同城；2 附近。按位置筛选时应配合 geo，实际上游定位要求由平台决定。', examples=[0])
    geo: str = Field(default='', max_length=1000,
                     description='上游地理位置参数字符串，默认空字符串；按位置筛选时传入上游认可的定位值。服务按原值转发，不解析或校验经纬度格式。', examples=[''])

    @model_validator(mode='after')
    def validate_context(self):
        if self.page > 1 and (not self.search_id or not self.search_session_id):
            raise ValueError('搜索翻页需要续传 search_id 和 search_session_id')
        return self


class XhsAccountRequest(XhsRequest):
    """短信和扫码共用账号标识校验。"""
    account_id: str = Field(min_length=1, max_length=64,
                            description='自定义账号池唯一标识，1～64 位小写字母、数字、下划线或连字符，例如 xhs_01；不是小红书用户 ID。同一账号再次登录时沿用，用于更新已有凭证。', examples=['xhs_01'])

    @model_validator(mode='after')
    def validate_account(self):
        validate_account_id(self.account_id)
        return self


class SmsSessionRequest(XhsAccountRequest):
    model_config = ConfigDict(json_schema_extra={'examples': [
        {'account_id': 'xhs_01', 'phone': '13800000000', 'zone': '86'},
    ]})
    phone: str = Field(pattern=r'^\d{5,15}$', description='接收验证码的手机号，5～15 位数字字符串；不含区号、加号或空格。请替换示例号码，重发时保持账号、手机号和区号一致。', examples=['13800000000'])
    zone: str = Field(default='86', pattern=r'^\d{1,4}$', description='国家或地区区号，1～4 位数字字符串，不含加号；默认 86 表示中国大陆。', examples=['86'])


class SmsLoginRequest(XhsRequest):
    """简化登录沿用发送验证码时返回的会话。"""
    model_config = ConfigDict(json_schema_extra={'examples': [
        {'session_id': '替换为发送接口返回的data.session_id', 'code': '123456'},
    ]})
    session_id: str = Field(min_length=1, max_length=64, description='发送验证码接口返回的 data.session_id，原样传入；仅用于本次短信登录，会话过期或服务重启后需重新发送验证码。', examples=['替换为发送接口返回的data.session_id'])
    code: str = Field(pattern=r'^\d{4,8}$', description='手机收到的 4～8 位数字验证码；使用字符串保留开头的 0，需对应本次 session_id。', examples=['123456'])


class QrSessionRequest(XhsAccountRequest):
    """创建小红书扫码登录会话。"""
    model_config = ConfigDict(json_schema_extra={'examples': [{'account_id': 'xhs_qr_01'}]})


class QrLoginRequest(XhsRequest):
    """提交扫码登录会话，服务端等待扫码完成。"""
    model_config = ConfigDict(json_schema_extra={'examples': [
        {'session_id': '替换为二维码接口返回的data.session_id'},
    ]})
    session_id: str = Field(min_length=1, max_length=64, description='二维码创建接口返回的 data.session_id，原样传入；服务端等待该二维码扫码及手机确认，会话过期或服务重启后需重新生成二维码。', examples=['替换为二维码接口返回的data.session_id'])

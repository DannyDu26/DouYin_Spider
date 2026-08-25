# coding=utf-8
import re
import unicodedata
from typing import Literal
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_URL_LENGTH = 2048


def _validate_douyin_url(value: str) -> str:
    """校验抖音链接，提前拒绝明显无效的请求。"""
    if len(value) > MAX_URL_LENGTH:
        raise ValueError(f'链接长度不能超过 {MAX_URL_LENGTH}')
    if any(unicodedata.category(character) == 'Cc' for character in value):
        raise ValueError('链接不能包含控制字符')
    parsed = urlparse(value)
    hostname = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not (
            hostname == 'douyin.com' or hostname.endswith('.douyin.com')):
        raise ValueError('必须提供有效的 HTTPS 抖音链接')
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('链接不能包含认证信息')
    return value


def _validate_work_url(value: str) -> str:
    """严格校验数字作品 ID，避免把参数错误传给上游。"""
    value = _validate_douyin_url(value)
    parsed = urlparse(value)
    if re.fullmatch(r'/video/\d+/?', parsed.path):
        return value
    modal_ids = parse_qs(parsed.query, keep_blank_values=True).get('modal_id', [])
    if len(modal_ids) == 1 and modal_ids[0].isdigit():
        return value
    raise ValueError('作品链接必须包含数字 /video/{id} 或 modal_id')


class WorksRequest(BaseModel):
    # 完整请求示例会显示在 Swagger 的请求体区域。
    model_config = ConfigDict(json_schema_extra={
        'examples': [{
            'urls': [
                'https://www.douyin.com/video/7517981045911538959',
                'https://www.douyin.com/video/7517946252007603508',
            ],
        }, {
            'video_id': '7517981045911538959',
        }],
    })

    urls: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description='抖音作品链接列表；每次可提交 1～20 个 HTTPS 链接。',
        examples=[['https://www.douyin.com/video/7517981045911538959']],
    )
    video_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r'^\d+$',
        description='单个抖音作品 ID；与 urls 二选一。',
        examples=['7517981045911538959'],
    )

    @field_validator('urls')
    @classmethod
    def validate_urls(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        for value in values:
            _validate_work_url(value)
        return values

    @model_validator(mode='after')
    def validate_work_locator(self):
        """链接列表和单个作品 ID 必须且只能提供一种。"""
        if (self.urls is None) == (self.video_id is None):
            raise ValueError('urls 和 video_id 必须且只能提供一个')
        return self

    @property
    def work_urls(self) -> list[str]:
        """将单个作品 ID 转换为批量抓取所需的规范链接列表。"""
        if self.urls is not None:
            return self.urls
        return [f'https://www.douyin.com/video/{self.video_id}']


class WorkCommentsRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        'examples': [{
            'video_id': '7517981045911538959',
            'cursor': 0,
            'count': 20,
        }],
    })

    url: str | None = Field(
        default=None,
        description='需要查询评论的抖音作品链接。',
        examples=['https://www.douyin.com/video/7517981045911538959'],
    )
    video_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r'^\d+$',
        description='抖音作品 ID；与 url 二选一。',
        examples=['7517981045911538959'],
    )
    cursor: int = Field(
        default=0,
        ge=0,
        le=9_223_372_036_854_775_807,
        strict=True,
        description='分页游标；首次请求传 0，后续使用响应中的 next_cursor。',
        examples=[0],
    )
    count: int = Field(
        default=20,
        ge=1,
        le=50,
        strict=True,
        description='本次请求的评论数量，范围为 1～50。',
        examples=[20],
    )

    @field_validator('url')
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        """评论接口仅接受经过严格校验的作品链接。"""
        if value is None:
            return None
        return _validate_work_url(value)

    @model_validator(mode='after')
    def validate_work_locator(self):
        """作品链接和作品 ID 必须且只能提供一个。"""
        if (self.url is None) == (self.video_id is None):
            raise ValueError('url 和 video_id 必须且只能提供一个')
        return self

    @property
    def work_url(self) -> str:
        """将作品 ID 转换为底层接口使用的规范链接。"""
        if self.url is not None:
            return self.url
        return f'https://www.douyin.com/video/{self.video_id}'


class VideoSubCommentsRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        'examples': [{
            'video_id': '7517981045911538959',
            'comment_id': '7518291639789450018',
            'cursor': 0,
            'count': 20,
        }],
    })

    video_id: str = Field(
        min_length=1,
        max_length=32,
        pattern=r'^\d+$',
        description='一级评论所属的抖音作品 ID。',
        examples=['7517981045911538959'],
    )
    comment_id: str = Field(
        min_length=1,
        max_length=32,
        pattern=r'^\d+$',
        description='需要查询回复的一级评论 ID。',
        examples=['7518291639789450018'],
    )
    cursor: int = Field(
        default=0,
        ge=0,
        le=9_223_372_036_854_775_807,
        strict=True,
        description='分页游标；首次请求传 0，后续使用响应中的 next_cursor。',
        examples=[0],
    )
    count: int = Field(
        default=20,
        ge=1,
        le=50,
        strict=True,
        description='本次请求的二级评论数量，范围为 1～50。',
        examples=[20],
    )


class UserWorksRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        'examples': [{
            'user_id': 'MS4wLjABAAAA-example',
            'page_num': 1,
        }],
    })

    user_url: str | None = Field(
        default=None,
        description='抖音用户主页链接，路径格式必须为 /user/{id}。',
        examples=['https://www.douyin.com/user/MS4wLjABAAAA-example'],
    )
    user_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r'^[A-Za-z0-9._~-]+$',
        description='用户主页 /user/{id} 路径中的用户 ID（sec_user_id）；与 user_url 二选一。',
        examples=['MS4wLjABAAAA-example'],
    )
    page_num: int = Field(
        default=1,
        ge=1,
        le=10,
        description='需要抓取的页数，范围为 1～10。',
        examples=[1],
    )

    @field_validator('user_url')
    @classmethod
    def validate_user_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _validate_douyin_url(value)
        if not re.fullmatch(r'/user/[^/]+/?', urlparse(value).path):
            raise ValueError('用户链接必须使用 /user/{id} 路径')
        return value

    @model_validator(mode='after')
    def validate_user_locator(self):
        """用户主页链接和用户 ID 必须且只能提供一个。"""
        if (self.user_url is None) == (self.user_id is None):
            raise ValueError('user_url 和 user_id 必须且只能提供一个')
        return self


class SearchWorksRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        'examples': [{
            'query': '榴莲',
            'limit': 20,
            'sort_type': '0',
            'publish_time': '0',
            'filter_duration': '',
            'search_range': '0',
            'content_type': '0',
        }],
    })

    query: str = Field(
        min_length=1,
        max_length=100,
        description='搜索关键词；首尾空白会被自动移除。',
        examples=['榴莲'],
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description='最多返回的作品数量，范围为 1～100。',
        examples=[20],
    )
    sort_type: Literal['0', '1', '2'] = Field(
        default='0',
        description='排序方式：0 综合排序；1 最多点赞；2 最新发布。',
    )
    publish_time: Literal['0', '1', '7', '180'] = Field(
        default='0',
        description='发布时间：0 不限；1 一天内；7 一周内；180 半年内。',
    )
    filter_duration: Literal['', '0-1', '1-5', '5-10000'] = Field(
        default='',
        description='视频时长：空字符串不限；0-1 一分钟内；1-5 一至五分钟；5-10000 五分钟以上。',
    )
    search_range: Literal['0', '1', '2', '3'] = Field(
        default='0',
        description='搜索范围：0 不限；1 最近看过；2 还未看过；3 关注的人。',
    )
    content_type: Literal['0', '1', '2'] = Field(
        default='0',
        description='内容类型：0 不限；1 视频；2 图文。',
    )
    target_account_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r'^[a-z0-9][a-z0-9_-]*$',
        description='仅供测试环境定向账号；需要显式启用测试账号固定开关。',
    )

    @field_validator('query')
    @classmethod
    def validate_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('query 不能为空')
        # 拒绝换行、ANSI 转义等控制字符，防止日志注入。
        if any(unicodedata.category(character) == 'Cc' for character in value):
            raise ValueError('query 不能包含控制字符')
        return value


class QrSessionRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        'examples': [{'account_id': 'marketing-01'}],
    })

    # remark 字段长度为 100，这里预留前缀和未来扩展空间
    account_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r'^[a-z0-9][a-z0-9_-]*$',
        description='账号唯一标识，只允许小写字母、数字、下划线和连字符。',
        examples=['marketing-01'],
    )


class QrSmsCodeRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        'examples': [{'code': '123456'}],
    })

    # 兼容抖音可能下发的 4～8 位数字验证码。
    code: str = Field(
        min_length=4,
        max_length=8,
        pattern=r'^\d{4,8}$',
        description='手机收到的短信验证码，仅允许 4～8 位数字。',
        examples=['123456'],
    )

"""统一接口文档分组和参数表，复用请求模型的字段定义。"""
import json

from fastapi.routing import APIRoute
from pydantic import BaseModel, TypeAdapter

XHS_LOGIN_TAG = '小红书 · 登录与账号'
XHS_DATA_TAG = '小红书 · 数据查询'
DOUYIN_LOGIN_TAG = '抖音 · 登录与账号'
DOUYIN_DATA_TAG = '抖音 · 数据查询'
SYSTEM_TAG = '系统 · 健康检查'
PARAMETER_DOCS_MARKER = '<!-- request-parameter-docs -->'
DOUYIN_QR_SESSION_DESCRIPTION = (
    '扫码登录会话 ID，取 POST /api/v1/douyin/auth/qr-sessions 返回的 data.session_id，'
    '原样替换 URL 中的 {session_id}；不是 account_id。查询、短信验证和取消均使用同一会话 ID；'
    '过期或服务重启后需重新创建会话。'
)

# 按平台排列常用登录和数据查询。
OPENAPI_TAGS = [
    {'name': XHS_LOGIN_TAG, 'description': '任选一种登录：短信发送验证码 → 提交验证码；扫码生成二维码 → 等待确认并登录。两种方式均无需客户端轮询，账号列表用于查看入池结果。'},
    {'name': XHS_DATA_TAG, 'description': '登录成功后查询笔记、用户和评论。分页接口每次返回一页，条目保留原始字段。'},
    {'name': DOUYIN_LOGIN_TAG, 'description': '创建扫码会话 → 展示二维码 → 查询状态，必要时提交短信验证码。'},
    {'name': DOUYIN_DATA_TAG, 'description': '查询作品详情、用户作品、关键词搜索和评论。'},
    {'name': SYSTEM_TAG, 'description': '检查服务、数据库和两个平台账号池的运行状态。'},
]

SWAGGER_UI_PARAMETERS = {
    'docExpansion': 'none',
    'filter': True,
    'defaultModelsExpandDepth': -1,
    'displayRequestDuration': True,
    'deepLinking': True,
}


def _table_cell(value):
    """转义 Markdown 表格字符，保留中文和 JSON 示例。"""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return value.replace('|', '\\|').replace('\r\n', '<br>').replace('\n', '<br>')


def _schema_type(schema):
    """使用 JSON 类型，明确字符串、整数、数组及可空字段。"""
    if 'anyOf' in schema:
        return ' / '.join(_schema_type(item) for item in schema['anyOf'])
    kind = schema.get('type', 'object')
    return f'array[{_schema_type(schema.get("items", {}))}]' if kind == 'array' else kind


def _parameter_table(rows):
    """所有接口共用参数表格式，避免描述与模型分别维护。"""
    header = '| 参数 | JSON 类型 | 必填 | 默认值 | 定义与用法 | 示例 |\n'
    header += '| --- | --- | --- | --- | --- | --- |'
    return header + ''.join('\n| ' + ' | '.join(_table_cell(cell) for cell in row) + ' |' for row in rows)


def append_parameter_docs(application):
    """把模型说明展开到接口描述中，即使隐藏 Schemas 也能查看参数。"""
    for route in application.routes:
        if not isinstance(route, APIRoute) or PARAMETER_DOCS_MARKER in (route.description or ''):
            continue
        sections = []
        for parameter in route.dependant.body_params:
            model = parameter.field_info.annotation
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                continue
            schema = model.model_json_schema()
            required = schema.get('required', [])
            sample = next(iter(schema.get('examples', [])), {})
            rows = []
            for name, field in schema.get('properties', {}).items():
                examples = field.get('examples', [])
                example = examples[0] if examples else sample.get(name, '—')
                rows.append((f'`{name}`', _schema_type(field), '是' if name in required else '否，见用法',
                             json.dumps(field['default'], ensure_ascii=False) if 'default' in field else '—',
                             field.get('description', ''), json.dumps(example, ensure_ascii=False) if example != '—' else '—'))
            sections.append('**请求体参数（JSON）**\n\n' + _parameter_table(rows))
        # 路径参数和请求体同时存在时，分别注明传参位置。
        for title, parameters in (
            ('路径参数', route.dependant.path_params), ('查询参数', route.dependant.query_params),
            ('请求头参数', route.dependant.header_params), ('Cookie 参数', route.dependant.cookie_params),
        ):
            if parameters:
                rows = [(f'`{item.alias}`', _schema_type(TypeAdapter(item.field_info.annotation).json_schema()), '是' if item.field_info.is_required() else '否',
                         '—' if item.field_info.is_required() else item.field_info.default, item.field_info.description or '',
                         next(iter(item.field_info.examples or []), '—')) for item in parameters]
                sections.append(f'**{title}**\n\n' + _parameter_table(rows))
        if not sections:
            sections.append('**请求参数**：无需传入请求体、路径或查询参数。')
        route.description = (route.description or '') + '\n\n' + PARAMETER_DOCS_MARKER + '\n\n' + '\n\n'.join(sections)
    # 清理可能已经生成的缓存，让 OpenAPI 使用补充后的描述。
    if hasattr(application, 'openapi_schema'):
        application.openapi_schema = None


def sms_response_docs(status, *, kind='sms', include_qrcode=False):
    """登录示例展示统一响应结构，避免默认显示无意义的 string。"""
    data = {
        'session_id': '本次登录会话ID', 'account_id': 'xhs_01', 'status': status,
        'created_at': '2026-10-12T00:00:00+00:00',
        'expires_at': '2026-10-12T00:05:00+00:00' if kind == 'sms' else '2026-10-12T00:03:00+00:00',
    }
    if kind == 'sms':
        data['phone_masked'] = '*******0000'
    if include_qrcode:
        data['qrcode_data_url'] = 'data:image/png;base64,...'
    return {
        200: {
            'description': '该步骤已完成。',
            'content': {'application/json': {
                'schema': {'type': 'object'},
                'example': {'success': True, 'request_id': '请求追踪ID', 'data': data},
            }},
        },
        404: {'description': '会话不存在或已清理。'},
        409: {'description': '当前会话正在处理其他操作，或账号已有登录会话。'},
        410: {'description': '会话已过期或取消，请重新开始登录。'},
    }

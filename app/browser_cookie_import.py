"""浏览器 Cookie 导入共用的格式校验和密钥鉴权。"""
import hmac
import os
import re


class BrowserCookieImportError(RuntimeError):
    """只向客户端返回预定义的导入错误，不携带原始凭证。"""
    def __init__(self, code, message, status_code):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def parse_browser_cookie(value):
    """拒绝控制字符、无效名称和重复字段，防止 Cookie 解析歧义。"""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Cookie 不能包含控制字符')
    cookies = {}
    for part in value.split(';'):
        name, separator, content = part.strip().partition('=')
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name):
            raise ValueError('Cookie 格式无效')
        if name in cookies:
            raise ValueError('Cookie 名称不能重复')
        cookies[name] = content.strip()
    return cookies


def require_import_token(supplied, platform, error_type=BrowserCookieImportError):
    """两个平台分别配置密钥；未配置时关闭对应导入入口。"""
    expected = os.getenv(f'{platform}_COOKIE_IMPORT_TOKEN', '').strip()
    if not expected:
        raise error_type(f'{platform}_COOKIE_IMPORT_DISABLED', '服务未启用浏览器 Cookie 导入', 503)
    if not hmac.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
        raise error_type(f'{platform}_COOKIE_IMPORT_UNAUTHORIZED', 'Cookie 导入密钥无效', 401)

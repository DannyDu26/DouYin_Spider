"""可安全暴露给 API 的小红书异常。"""


class XhsApiError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class XhsAuthenticationError(XhsApiError):
    def __init__(self):
        # 登录过期单独返回 401，便于调用方触发重新登录。
        super().__init__('XHS_AUTH_EXPIRED', '小红书账号登录已过期，请重新登录', 401)


class XhsRiskControlError(XhsApiError):
    def __init__(self, signal: str, retry_after_seconds: int | None = None):
        details = {'signal': signal}
        if retry_after_seconds is not None:
            details['retry_after_seconds'] = retry_after_seconds
        super().__init__('XHS_RISK_CONTROL', '小红书上游要求安全验证或限制访问', 429, details)

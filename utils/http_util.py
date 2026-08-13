# coding=utf-8
"""共享的抖音 HTTP 超时配置。"""

import math
import os
from pathlib import Path


CONNECT_TIMEOUT_ENV = 'DOUYIN_CONNECT_TIMEOUT_SECONDS'
READ_TIMEOUT_ENV = 'DOUYIN_READ_TIMEOUT_SECONDS'
CA_BUNDLE_ENV = 'DOUYIN_CA_BUNDLE'
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_READ_TIMEOUT_SECONDS = 30.0


def _positive_timeout_from_env(name: str, default: float) -> float:
    """读取有限正数，避免请求无限期阻塞。"""
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as error:
        raise RuntimeError(f'{name} 必须是有限正数') from error
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f'{name} 必须是有限正数')
    return value


def get_douyin_http_timeout() -> tuple[float, float]:
    """返回 requests 使用的（连接超时，读取超时）。"""
    return (
        _positive_timeout_from_env(CONNECT_TIMEOUT_ENV, DEFAULT_CONNECT_TIMEOUT_SECONDS),
        _positive_timeout_from_env(READ_TIMEOUT_ENV, DEFAULT_READ_TIMEOUT_SECONDS),
    )


def get_douyin_tls_verify() -> bool | str:
    """默认启用系统 CA 校验，也可指定内部 CA 文件。"""
    raw_path = os.getenv(CA_BUNDLE_ENV, '').strip()
    if not raw_path:
        return True
    ca_path = Path(raw_path).expanduser()
    if not ca_path.is_file():
        raise RuntimeError(f'{CA_BUNDLE_ENV} 必须指向存在的 CA 文件')
    return str(ca_path.resolve())


__all__ = [
    'CONNECT_TIMEOUT_ENV',
    'READ_TIMEOUT_ENV',
    'CA_BUNDLE_ENV',
    'DEFAULT_CONNECT_TIMEOUT_SECONDS',
    'DEFAULT_READ_TIMEOUT_SECONDS',
    'get_douyin_http_timeout',
    'get_douyin_tls_verify',
]

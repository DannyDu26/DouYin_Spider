"""小红书业务请求的统一节流和临时故障退避。"""
import math
import os
import random
import threading
import time
from email.utils import parsedate_to_datetime

from loguru import logger

from xhs_apis.errors import XhsApiError


def retry_after_seconds(response, now=None):
    """兼容秒数和 HTTP 日期；异常头不影响原有错误分类。"""
    headers = getattr(response, 'headers', {})
    value = str(headers.get('Retry-After', headers.get('retry-after', ''))).strip()
    try:
        if value.isascii() and value.isdigit():
            return int(value)
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            return None
        return max(0, math.ceil(target.timestamp() - (time.time() if now is None else now)))
    except (ValueError, TypeError, OverflowError):
        return None


def _number_from_env(name, default, *, integer=False):
    # 拒绝 NaN、无穷及负数，避免配置绕过节流或导致无限等待。
    try:
        value = int(os.getenv(name, str(default))) if integer else float(os.getenv(name, str(default)))
        if not math.isfinite(value) or value < 0:
            raise ValueError
    except (ValueError, OverflowError) as error:
        kind = '非负整数' if integer else '有限非负数'
        raise RuntimeError(f'{name} 必须是{kind}') from error
    return value


class XhsRequestPolicy:
    def __init__(self, interval=5, jitter=5, acquire_timeout=30, retries=2,
                 backoff=5, backoff_max=60, *, clock=None, sleep=None, uniform=None):
        values = (interval, jitter, acquire_timeout, retries, backoff, backoff_max)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError('小红书请求策略参数必须是有限非负数')
        if acquire_timeout <= 0 or backoff <= 0 or backoff_max < backoff:
            raise ValueError('排队时间和退避基数必须大于零，退避上限不得小于基数')
        if type(retries) is not int or retries > 10:
            raise ValueError('小红书重试次数必须是 0～10 的整数')
        self.interval, self.jitter = interval, jitter
        self.acquire_timeout, self.retries = acquire_timeout, retries
        self.backoff, self.backoff_max = backoff, backoff_max
        self._clock, self._sleep = clock or time.monotonic, sleep or time.sleep
        self._uniform = uniform or random.uniform
        self._lock = threading.Lock()
        self._next_start = 0
        self._unavailable_until = 0
        self._failures = 0

    @classmethod
    def from_env(cls):
        return cls(
            interval=_number_from_env('XHS_REQUEST_INTERVAL_SECONDS', 5),
            jitter=_number_from_env('XHS_REQUEST_JITTER_SECONDS', 5),
            acquire_timeout=_number_from_env('XHS_REQUEST_QUEUE_TIMEOUT_SECONDS', 30),
            retries=_number_from_env('XHS_UPSTREAM_MAX_RETRIES', 2, integer=True),
            backoff=_number_from_env('XHS_UPSTREAM_BACKOFF_SECONDS', 5),
            backoff_max=_number_from_env('XHS_UPSTREAM_BACKOFF_MAX_SECONDS', 60),
        )

    def _unavailable(self):
        return XhsApiError('XHS_UPSTREAM_UNAVAILABLE', '小红书上游暂时不可用，请等待后重试', 503,
                           {'http_status': 503, 'reason': 'upstream_backoff',
                            'retry_after_seconds': max(1, math.ceil(self._unavailable_until - self._clock()))})

    @staticmethod
    def queue_full(wait=1):
        return XhsApiError('XHS_REQUEST_QUEUE_FULL', '小红书请求排队超时，请降低调用频率', 503,
                           {'reason': 'queue_timeout', 'retry_after_seconds': max(1, math.ceil(wait))})

    def execute(self, send):
        """锁覆盖签名及发送；同进程各账号、接口共用一个节流窗口。"""
        deadline = self._clock() + self.acquire_timeout
        if not self._lock.acquire(timeout=self.acquire_timeout):
            raise self.queue_full()
        try:
            # 长时间退避直接告知调用方，避免所有工作线程一起睡眠。
            if self._unavailable_until > self._clock():
                raise self._unavailable()
            # 已熔断的请求只做一次恢复探测，失败后立即延长退避。
            retry_limit = 0 if self._failures else self.retries
            for attempt in range(retry_limit + 1):
                wait = max(0, self._next_start - self._clock(), self._unavailable_until - self._clock())
                if wait > max(0, deadline - self._clock()):
                    if self._unavailable_until > self._clock():
                        raise self._unavailable()
                    raise self.queue_full(wait)
                if wait:
                    self._sleep(wait)
                # 等待结束后才签名，每次重试均生成新的时间戳和序号。
                try:
                    response = send()
                finally:
                    # 从请求结束开始计时，签名耗时差异不会压缩实际请求间隔。
                    self._next_start = self._clock() + self.interval + self._uniform(0, self.jitter)
                if response.status_code != 503:
                    self._failures = 0
                    self._unavailable_until = 0
                    return response
                self._failures += 1
                delay = min(self.backoff_max, self.backoff * 2 ** min(self._failures - 1, 16))
                delay = min(self.backoff_max, delay + self._uniform(0, self.jitter))
                delay = max(delay, retry_after_seconds(response) or 0)
                self._unavailable_until = self._clock() + delay
                logger.warning('小红书上游 HTTP 503，退避秒数={} 本次请求次数={}', delay, attempt + 1)
                if attempt == retry_limit:
                    raise self._unavailable()
        finally:
            self._lock.release()

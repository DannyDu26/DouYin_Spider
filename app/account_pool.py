# coding=utf-8
"""线程安全的抖音账号轮询池。"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.account_store import CredentialRecord, MySQLCredentialStore, validate_account_id


AVAILABLE = 'available'
COOLING = 'cooling'
INVALID = 'invalid'


class NoAvailableAccountError(RuntimeError):
    """当前没有可供请求使用的账号。"""

    def __init__(self, retry_after_seconds: int | None = None):
        super().__init__('当前没有可用的抖音账号')
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class AccountLease:
    """一次请求固定使用的认证快照。"""

    account_id: str
    auth: Any
    row_id: int
    created_at: datetime | None

    @property
    def credential_id(self) -> int:
        return self.row_id


@dataclass(slots=True)
class _AccountState:
    record: CredentialRecord
    semaphore: threading.BoundedSemaphore
    cooldown_until: float | None = None
    failure_count: int = 0


def _positive_int_from_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f'{name} 必须是正整数') from error
    if value <= 0:
        raise RuntimeError(f'{name} 必须是正整数')
    return value


def _non_negative_int_from_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f'{name} 必须是非负整数') from error
    if value < 0:
        raise RuntimeError(f'{name} 必须是非负整数')
    return value


def _non_negative_float_from_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f'{name} 必须是非负数') from error
    if value < 0:
        raise RuntimeError(f'{name} 必须是非负数')
    return value


def _iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _iso_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace('+00:00', 'Z')


class AccountPool:
    """管理账号状态、轮询顺序和账号级并发。"""

    def __init__(
        self,
        records: Iterable[CredentialRecord] = (),
        max_concurrent_per_account: int = 1,
        cooldown_seconds: float = 300,
        cooldown_failure_limit: int = 3,
        credential_remover: Callable[[CredentialRecord], bool] | None = None,
        clock=None,
    ):
        if max_concurrent_per_account <= 0:
            raise ValueError('max_concurrent_per_account 必须是正整数')
        if cooldown_seconds < 0:
            raise ValueError('cooldown_seconds 必须是非负数')
        if cooldown_failure_limit < 0:
            raise ValueError('cooldown_failure_limit 必须是非负整数')
        self.max_concurrent_per_account = max_concurrent_per_account
        self.cooldown_seconds = float(cooldown_seconds)
        self.cooldown_failure_limit = cooldown_failure_limit
        self._credential_remover = credential_remover
        self._clock = clock or time.time
        self._condition = threading.Condition(threading.RLock())
        self._accounts: dict[str, _AccountState] = {}
        # 记录已移除的凭证版本，避免定时刷新重新加入同一失效凭证。
        self._removed_credentials: dict[str, int] = {}
        self._cursor = 0
        for record in records:
            self._upsert_record(record)

    @classmethod
    def from_store(
        cls,
        store: MySQLCredentialStore,
        max_concurrent_per_account: int | None = None,
        cooldown_seconds: float | None = None,
        cooldown_failure_limit: int | None = None,
    ) -> 'AccountPool':
        """加载数据库最新版本并创建运行时账号池。"""
        if max_concurrent_per_account is None:
            max_concurrent_per_account = _positive_int_from_env(
                'MAX_CONCURRENT_REQUESTS_PER_ACCOUNT', 1
            )
        if cooldown_seconds is None:
            cooldown_seconds = _non_negative_float_from_env('ACCOUNT_COOLDOWN_SECONDS', 300)
        if cooldown_failure_limit is None:
            cooldown_failure_limit = _non_negative_int_from_env(
                'ACCOUNT_COOLDOWN_FAILURE_LIMIT', 3
            )
        return cls(
            store.load_latest(),
            max_concurrent_per_account=max_concurrent_per_account,
            cooldown_seconds=cooldown_seconds,
            cooldown_failure_limit=cooldown_failure_limit,
            credential_remover=getattr(store, 'delete_credential', None),
        )

    def _upsert_record(self, record: CredentialRecord) -> None:
        # 显式写入来自扫码提交，可恢复更新了同一数据库行的凭证。
        self._removed_credentials.pop(record.account_id, None)
        state = self._accounts.get(record.account_id)
        if state is None:
            self._accounts[record.account_id] = _AccountState(
                record=record,
                semaphore=threading.BoundedSemaphore(self.max_concurrent_per_account),
            )
            return
        # 保留正在使用的信号量，使旧租约结束后再放行新认证。
        state.record = record
        state.cooldown_until = None
        state.failure_count = 0

    def upsert(
        self,
        record_or_account_id: CredentialRecord | str,
        auth: Any = None,
        record: CredentialRecord | None = None,
    ) -> CredentialRecord:
        """原子新增或刷新账号；兼容扫码服务的三参数调用。"""
        if isinstance(record_or_account_id, CredentialRecord):
            if auth is not None or record is not None:
                raise TypeError('传入 CredentialRecord 时不能再传 auth 或 record')
            final_record = record_or_account_id
        else:
            account_id = validate_account_id(record_or_account_id)
            if record is None:
                final_record = CredentialRecord(
                    row_id=0,
                    account_id=account_id,
                    created_at=datetime.now(timezone.utc),
                    auth=auth,
                )
            else:
                final_record = replace(
                    record,
                    account_id=account_id,
                    auth=auth if auth is not None else record.auth,
                    invalid_reason=None if (auth is not None or record.auth is not None) else record.invalid_reason,
                )

        with self._condition:
            self._upsert_record(final_record)
            self._condition.notify_all()
        return final_record

    def refresh(self, records: Iterable[CredentialRecord]) -> int:
        """合并数据库中的新版本，不覆盖扫码刚写入的更新版本。"""
        changed = 0
        with self._condition:
            for record in records:
                state = self._accounts.get(record.account_id)
                if state is None:
                    removed_credential_id = self._removed_credentials.get(record.account_id)
                    if removed_credential_id is not None:
                        if record.row_id <= removed_credential_id:
                            continue
                        # 更高版本凭证视为重新登录成功，可以恢复账号。
                        self._removed_credentials.pop(record.account_id, None)
                    self._accounts[record.account_id] = _AccountState(
                        record=record,
                        semaphore=threading.BoundedSemaphore(self.max_concurrent_per_account),
                    )
                    changed += 1
                    continue

                # 定时查询可能早于并发扫码 INSERT，禁止旧记录回写内存池。
                if record.row_id <= state.record.row_id:
                    continue
                # 保留信号量，使已租出的旧认证可以安全完成当前请求。
                state.record = record
                state.cooldown_until = None
                state.failure_count = 0
                changed += 1

            if changed:
                self._condition.notify_all()
        return changed

    def _status(self, state: _AccountState, now: float) -> str:
        if not state.record.is_valid:
            return INVALID
        if state.cooldown_until is not None:
            if state.cooldown_until > now:
                return COOLING
            state.cooldown_until = None
        return AVAILABLE

    @staticmethod
    def _normalize_excluded(exclude: str | Iterable[str] | None) -> set[str]:
        if exclude is None:
            return set()
        if isinstance(exclude, str):
            return {exclude}
        return set(exclude)

    def _retry_after(self, now: float) -> int | None:
        """计算整个账号池最早恢复时间，包括本次已切换掉的账号。"""
        remaining = [
            state.cooldown_until - now
            for state in self._accounts.values()
            if state.record.is_valid
            and state.cooldown_until is not None
            and state.cooldown_until > now
        ]
        if not remaining:
            return None
        return max(1, math.ceil(min(remaining)))

    def _take_lease(
        self,
        excluded: set[str],
        timeout: float | None,
        account_id: str | None = None,
    ) -> tuple[AccountLease, _AccountState]:
        if timeout is not None and timeout < 0:
            raise ValueError('timeout 不能为负数')
        deadline = None if timeout is None else time.monotonic() + timeout

        with self._condition:
            while True:
                now = self._clock()
                # 指定账号时只等待该账号的槽位，禁止悄悄切换到其他账号。
                account_ids = [account_id] if account_id is not None else list(self._accounts)
                available_but_busy = False
                if account_ids:
                    start = 0 if account_id is not None else self._cursor % len(account_ids)
                    for offset in range(len(account_ids)):
                        index = (start + offset) % len(account_ids)
                        selected_account_id = account_ids[index]
                        state = self._accounts.get(selected_account_id)
                        if state is None:
                            continue
                        if selected_account_id in excluded or self._status(state, now) != AVAILABLE:
                            continue
                        if state.semaphore.acquire(blocking=False):
                            if account_id is None:
                                self._cursor = (index + 1) % len(account_ids)
                            record = state.record
                            return AccountLease(
                                account_id=selected_account_id,
                                auth=record.auth,
                                row_id=record.row_id,
                                created_at=record.created_at,
                            ), state
                        available_but_busy = True

                if not available_but_busy:
                    raise NoAvailableAccountError(self._retry_after(now))

                wait_seconds = None
                if deadline is not None:
                    wait_seconds = deadline - time.monotonic()
                    if wait_seconds <= 0:
                        raise NoAvailableAccountError()
                self._condition.wait(wait_seconds)

    @contextmanager
    def acquire(
        self,
        exclude: str | Iterable[str] | None = None,
        timeout: float | None = None,
        account_id: str | None = None,
    ) -> Iterator[AccountLease]:
        """租用一个账号；上下文退出时自动释放账号级并发槽位。"""
        if account_id is not None:
            validate_account_id(account_id)
        lease, state = self._take_lease(
            self._normalize_excluded(exclude),
            timeout,
            account_id=account_id,
        )
        try:
            yield lease
        finally:
            state.semaphore.release()
            with self._condition:
                self._condition.notify_all()

    def mark_auth_failure(
        self,
        account_id: str,
        credential_id: int | None = None,
        credential_auth: Any = None,
    ) -> datetime | None:
        """仅冷却发生失败的凭证版本，避免误伤刚刷新的账号。"""
        removed_record = None
        with self._condition:
            state = self._accounts.get(account_id)
            if state is None:
                removed_credential_id = self._removed_credentials.get(account_id)
                # 并发旧租约可能在账号移除后才上报失败，保持幂等即可。
                if (
                        removed_credential_id is not None
                        and credential_id is not None
                        and credential_id <= removed_credential_id
                ):
                    return None
                raise KeyError(account_id)
            if credential_id is not None and state.record.row_id != credential_id:
                return None
            # 数据库更新可能复用行 ID，认证对象也必须仍是同一租约快照。
            if credential_auth is not None and state.record.auth is not credential_auth:
                return None
            state.failure_count += 1
            state.cooldown_until = self._clock() + self.cooldown_seconds
            cooldown_until = state.cooldown_until
            if (
                    self.cooldown_failure_limit > 0
                    and state.failure_count >= self.cooldown_failure_limit
            ):
                # 只移除当前凭证版本；更高版本凭证仍可重新入池。
                removed_record = state.record
                self._removed_credentials[account_id] = state.record.row_id
                del self._accounts[account_id]
                if self._accounts:
                    self._cursor %= len(self._accounts)
                else:
                    self._cursor = 0
            self._condition.notify_all()
        if removed_record is not None and self._credential_remover is not None:
            try:
                deleted = self._credential_remover(removed_record)
                logger.info(
                    '账号达到冷却阈值，数据库凭证删除完成 account_id={} credential_id={} deleted={}',
                    removed_record.account_id,
                    removed_record.row_id,
                    deleted,
                )
            except Exception as error:
                # 内存墓碑继续阻止旧凭证回池，数据库异常不覆盖原始风控响应。
                logger.error(
                    '账号达到冷却阈值，但数据库凭证删除失败 account_id={} credential_id={} error_type={}',
                    removed_record.account_id,
                    removed_record.row_id,
                    error.__class__.__name__,
                )
        return datetime.fromtimestamp(cooldown_until, tz=timezone.utc)

    def mark_risk_control(
        self,
        account_id: str,
        credential_id: int | None = None,
        credential_auth: Any = None,
    ) -> datetime | None:
        """风控只冷却实际使用的账号，沿用统一冷却窗口。"""
        return self.mark_auth_failure(account_id, credential_id, credential_auth)

    def retry_after_seconds(self) -> int | None:
        """返回任一冷却账号最早恢复所需秒数。"""
        with self._condition:
            return self._retry_after(self._clock())

    def stats(self) -> dict[str, int]:
        """返回可安全公开的账号状态计数。"""
        counts = {'total': 0, AVAILABLE: 0, COOLING: 0, INVALID: 0}
        with self._condition:
            now = self._clock()
            counts['total'] = len(self._accounts)
            for state in self._accounts.values():
                counts[self._status(state, now)] += 1
        return counts

    def list_accounts(self) -> list[dict[str, Any]]:
        """返回不含任何 Cookie 或票据的账号元数据。"""
        accounts: list[dict[str, Any]] = []
        with self._condition:
            now = self._clock()
            for account_id in sorted(self._accounts):
                state = self._accounts[account_id]
                record = state.record
                status = self._status(state, now)
                accounts.append({
                    'account_id': account_id,
                    'credential_id': record.row_id,
                    'updated_at': _iso_datetime(record.created_at),
                    'status': status,
                    'cooldown_until': _iso_timestamp(state.cooldown_until),
                })
        return accounts


__all__ = [
    'AVAILABLE',
    'COOLING',
    'INVALID',
    'AccountLease',
    'AccountPool',
    'NoAvailableAccountError',
]

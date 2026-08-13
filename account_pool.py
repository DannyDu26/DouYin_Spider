# coding=utf-8
"""线程安全的抖音账号轮询池。"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from account_store import CredentialRecord, MySQLCredentialStore, validate_account_id


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
        clock=None,
    ):
        if max_concurrent_per_account <= 0:
            raise ValueError('max_concurrent_per_account 必须是正整数')
        if cooldown_seconds < 0:
            raise ValueError('cooldown_seconds 必须是非负数')
        self.max_concurrent_per_account = max_concurrent_per_account
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock or time.time
        self._condition = threading.Condition(threading.RLock())
        self._accounts: dict[str, _AccountState] = {}
        self._cursor = 0
        for record in records:
            self._upsert_record(record)

    @classmethod
    def from_store(
        cls,
        store: MySQLCredentialStore,
        max_concurrent_per_account: int | None = None,
        cooldown_seconds: float | None = None,
    ) -> 'AccountPool':
        """加载数据库最新版本并创建运行时账号池。"""
        if max_concurrent_per_account is None:
            max_concurrent_per_account = _positive_int_from_env(
                'MAX_CONCURRENT_REQUESTS_PER_ACCOUNT', 1
            )
        if cooldown_seconds is None:
            cooldown_seconds = _non_negative_float_from_env('ACCOUNT_COOLDOWN_SECONDS', 300)
        return cls(
            store.load_latest(),
            max_concurrent_per_account=max_concurrent_per_account,
            cooldown_seconds=cooldown_seconds,
        )

    def _upsert_record(self, record: CredentialRecord) -> None:
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
    ) -> tuple[AccountLease, _AccountState]:
        if timeout is not None and timeout < 0:
            raise ValueError('timeout 不能为负数')
        deadline = None if timeout is None else time.monotonic() + timeout

        with self._condition:
            while True:
                now = self._clock()
                account_ids = list(self._accounts)
                available_but_busy = False
                if account_ids:
                    start = self._cursor % len(account_ids)
                    for offset in range(len(account_ids)):
                        index = (start + offset) % len(account_ids)
                        account_id = account_ids[index]
                        state = self._accounts[account_id]
                        if account_id in excluded or self._status(state, now) != AVAILABLE:
                            continue
                        if state.semaphore.acquire(blocking=False):
                            self._cursor = (index + 1) % len(account_ids)
                            record = state.record
                            return AccountLease(
                                account_id=account_id,
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
    ) -> Iterator[AccountLease]:
        """租用一个账号；上下文退出时自动释放账号级并发槽位。"""
        lease, state = self._take_lease(self._normalize_excluded(exclude), timeout)
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
    ) -> datetime | None:
        """仅冷却发生失败的凭证版本，避免误伤刚刷新的账号。"""
        with self._condition:
            state = self._accounts.get(account_id)
            if state is None:
                raise KeyError(account_id)
            if credential_id is not None and state.record.row_id != credential_id:
                return None
            state.failure_count += 1
            state.cooldown_until = self._clock() + self.cooldown_seconds
            self._condition.notify_all()
            return datetime.fromtimestamp(state.cooldown_until, tz=timezone.utc)

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

# coding=utf-8
"""多账号关键词搜索并发风控测试。"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import re
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.test_account_concurrency import (
    ACCOUNTS_PATH,
    DEFAULT_LEVELS,
    HEALTH_PATH,
    SEARCH_PATH,
    PreflightError,
    _envelope_data,
    _safe_signal,
    fetch_accounts,
    non_negative_float,
    normalize_base_url,
    parse_levels,
    percentile,
    positive_float,
    utc_now,
)


ACCOUNT_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')


@dataclass(frozen=True)
class MultiTestConfig:
    base_url: str
    account_ids: tuple[str, ...]
    query: str
    mode: str = 'sequential'
    concurrency_scope: str = 'per-account'
    levels: tuple[int, ...] = DEFAULT_LEVELS
    stage_seconds: float = 60.0
    rps_per_worker: float = 1.0
    cooldown_seconds: float = 120.0
    observation_seconds: float = 300.0
    request_timeout: float = 45.0


@dataclass
class RequestRecord:
    stage: int
    configured_concurrency: int
    configured_account_concurrency: int
    configured_total_concurrency: int
    sequence: int
    started_at: str
    latency_ms: float
    http_status: int | None
    app_code: str
    outcome: str
    target_account_id: str
    actual_account_id: str
    request_id: str
    in_flight: int
    account_in_flight: int


@dataclass
class MultiRunState:
    account_ids: tuple[str, ...]
    records: list[RequestRecord] = field(default_factory=list)
    stage_summaries: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    in_flight: int = 0
    peak_in_flight: int = 0
    next_sequence: int = 1
    stop_kind: str | None = None
    stop_reason: str | None = None
    stop_account_id: str | None = None
    trigger_level: int | None = None
    safe_level: int | None = None
    last_level: int | None = None
    global_consecutive_errors: int = 0
    global_rolling_errors: deque[bool] = field(
        default_factory=lambda: deque(maxlen=20)
    )
    account_in_flight: dict[str, int] = field(init=False)
    account_consecutive_errors: dict[str, int] = field(init=False)
    account_rolling_errors: dict[str, deque[bool]] = field(init=False)
    account_results: dict[str, dict[str, Any]] = field(init=False)
    passed_concurrencies: dict[str, list[int]] = field(init=False)
    passed_levels: list[int] = field(default_factory=list)
    last_allocations: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.account_in_flight = {account_id: 0 for account_id in self.account_ids}
        self.account_consecutive_errors = {
            account_id: 0 for account_id in self.account_ids
        }
        self.account_rolling_errors = {
            account_id: deque(maxlen=20) for account_id in self.account_ids
        }
        self.account_results = {
            account_id: {
                'safe_concurrency': None,
                'trigger_concurrency': None,
            }
            for account_id in self.account_ids
        }
        self.passed_concurrencies = {
            account_id: [] for account_id in self.account_ids
        }

    async def begin_request(self, account_id: str) -> tuple[int, int, int]:
        """原子记录全局和账号维度的在途请求。"""
        async with self.lock:
            sequence = self.next_sequence
            self.next_sequence += 1
            self.in_flight += 1
            self.account_in_flight[account_id] += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            return sequence, self.in_flight, self.account_in_flight[account_id]

    async def finish_request(self, record: RequestRecord) -> None:
        """记录结果，并同时检查账号级和全局异常窗口。"""
        async with self.lock:
            self.in_flight -= 1
            self.account_in_flight[record.target_account_id] -= 1
            self.records.append(record)

            ordinary_error = record.outcome in {
                'transport_error',
                'upstream_error',
                'unexpected_response',
            }
            account_id = record.target_account_id
            self.global_rolling_errors.append(ordinary_error)
            self.account_rolling_errors[account_id].append(ordinary_error)
            if ordinary_error:
                self.global_consecutive_errors += 1
                self.account_consecutive_errors[account_id] += 1
            else:
                self.global_consecutive_errors = 0
                self.account_consecutive_errors[account_id] = 0

            if record.outcome == 'risk_control':
                self._stop(
                    'risk',
                    record.app_code or 'risk_control',
                    record.configured_concurrency,
                    account_id,
                    record.configured_account_concurrency,
                )
            elif record.outcome in {'account_mismatch', 'configuration_error'}:
                self._stop(
                    'isolation',
                    record.app_code or record.outcome,
                    record.configured_concurrency,
                    account_id,
                    record.configured_account_concurrency,
                )
            elif (
                    self.account_consecutive_errors[account_id] >= 3
                    or self.global_consecutive_errors >= 3
            ):
                self._stop(
                    'degradation',
                    'three_consecutive_errors',
                    record.configured_concurrency,
                    account_id,
                    record.configured_account_concurrency,
                )
            elif self._rolling_error_limit_reached(account_id):
                self._stop(
                    'degradation',
                    'rolling_error_rate',
                    record.configured_concurrency,
                    account_id,
                    record.configured_account_concurrency,
                )

    def _rolling_error_limit_reached(self, account_id: str) -> bool:
        account_window = self.account_rolling_errors[account_id]
        account_failed = (
            len(account_window) == 20
            and sum(account_window) / len(account_window) >= 0.05
        )
        global_failed = (
            len(self.global_rolling_errors) == 20
            and sum(self.global_rolling_errors) / len(self.global_rolling_errors) >= 0.05
        )
        return account_failed or global_failed

    def clear_error_windows(self) -> None:
        """冒烟请求不参与正式阶段的错误率统计。"""
        self.global_consecutive_errors = 0
        self.global_rolling_errors.clear()
        for account_id in self.account_ids:
            self.account_consecutive_errors[account_id] = 0
            self.account_rolling_errors[account_id].clear()

    def mark_passed(self, account_id: str, concurrency: int) -> None:
        self.passed_concurrencies[account_id].append(concurrency)
        self.account_results[account_id]['safe_concurrency'] = max(
            self.passed_concurrencies[account_id]
        )

    def _stop(
            self,
            kind: str,
            reason: str,
            level: int | None,
            account_id: str | None = None,
            account_concurrency: int | None = None,
    ) -> None:
        """只保留首个停止原因，确保报告结论稳定。"""
        if self.stop_kind is not None:
            return
        self.stop_kind = kind
        self.stop_reason = reason
        self.stop_account_id = account_id
        self.trigger_level = level
        if account_id in self.account_results:
            self.account_results[account_id]['trigger_concurrency'] = account_concurrency
        self.stop_event.set()


def parse_account_ids(raw_value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in raw_value.split(',') if item.strip())
    if len(values) < 2:
        raise argparse.ArgumentTypeError('多账号测试至少需要两个账号 ID')
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError('账号 ID 不能重复')
    if any(not ACCOUNT_ID_PATTERN.fullmatch(value) for value in values):
        raise argparse.ArgumentTypeError('账号 ID 格式无效')
    return values


def allocation_for(
        config: MultiTestConfig,
        level: int,
        stage: int,
        sequential_account_id: str | None = None,
) -> dict[str, int]:
    """将阶段并发公平分配到目标账号。"""
    if sequential_account_id is not None:
        return {account_id: level if account_id == sequential_account_id else 0
                for account_id in config.account_ids}
    if config.concurrency_scope == 'per-account':
        return {account_id: level for account_id in config.account_ids}

    count = len(config.account_ids)
    base, remainder = divmod(level, count)
    start = (stage - 1) % count
    allocations = {account_id: base for account_id in config.account_ids}
    for offset in range(remainder):
        allocations[config.account_ids[(start + offset) % count]] += 1
    return allocations


async def preflight(client: httpx.AsyncClient, config: MultiTestConfig) -> None:
    """确认账号集合、定向开关和两级并发闸门。"""
    try:
        health = _envelope_data(await client.get(HEALTH_PATH), '健康检查')
    except httpx.HTTPError as error:
        raise PreflightError('健康检查请求失败') from error
    if health.get('status') != 'ok':
        raise PreflightError('服务状态不是 ok')
    if health.get('test_account_pinning_enabled') is not True:
        raise PreflightError('服务未启用测试账号定向能力')

    highest = max(config.levels)
    if config.mode == 'simultaneous' and config.concurrency_scope == 'per-account':
        required_global = highest * len(config.account_ids)
        required_per_account = highest
    elif config.mode == 'simultaneous':
        required_global = highest
        required_per_account = math.ceil(highest / len(config.account_ids))
    else:
        required_global = highest
        required_per_account = highest

    global_limit = health.get('max_concurrent_requests')
    account_limit = health.get('max_concurrent_requests_per_account')
    if not isinstance(global_limit, int) or global_limit < required_global:
        raise PreflightError(f'全局并发上限低于测试所需 {required_global}')
    if not isinstance(account_limit, int) or account_limit < required_per_account:
        raise PreflightError(f'单账号并发上限低于测试所需 {required_per_account}')

    try:
        accounts = await fetch_accounts(client)
    except httpx.HTTPError as error:
        raise PreflightError('账号列表请求失败') from error
    actual_ids = {str(item.get('account_id') or '') for item in accounts}
    if actual_ids != set(config.account_ids) or len(accounts) != len(config.account_ids):
        raise PreflightError('测试实例账号集合与 --account-ids 不一致')
    unavailable = [
        str(item.get('account_id'))
        for item in accounts
        if item.get('status') != 'available'
    ]
    if unavailable:
        raise PreflightError('存在不可用的目标账号')


def classify_response(
        response: httpx.Response,
        expected_account_id: str,
) -> tuple[str, str, str, str]:
    """返回结果分类、应用码、实际账号和请求 ID。"""
    request_id = response.headers.get('X-Request-ID', '')
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get('error') if isinstance(body, dict) else None
    app_code = error.get('code', '') if isinstance(error, dict) else ''

    if app_code == 'ACCOUNT_PINNING_DISABLED':
        return 'configuration_error', app_code, '', request_id
    if response.status_code == 429 or app_code == 'UPSTREAM_RISK_CONTROL':
        return 'risk_control', _safe_signal(body), expected_account_id, request_id
    if response.status_code == 403:
        return 'risk_control', 'http_403', expected_account_id, request_id
    if response.status_code == 503 and app_code == 'NO_AVAILABLE_ACCOUNT':
        return 'risk_control', 'account_unavailable', expected_account_id, request_id
    if response.status_code == 502:
        return 'upstream_error', app_code or 'UPSTREAM_ERROR', '', request_id

    data = body.get('data') if isinstance(body, dict) else None
    if (
            response.status_code == 200
            and isinstance(body, dict)
            and body.get('success') is True
            and isinstance(data, dict)
    ):
        actual_account_id = str(data.get('account_id') or '')
        if actual_account_id != expected_account_id:
            return 'account_mismatch', 'ACCOUNT_MISMATCH', actual_account_id, request_id
        return 'success', '', actual_account_id, request_id
    return 'unexpected_response', app_code or f'HTTP_{response.status_code}', '', request_id


def search_payload(config: MultiTestConfig, account_id: str) -> dict[str, Any]:
    return {
        'query': config.query,
        'limit': 1,
        'sort_type': '0',
        'publish_time': '0',
        'filter_duration': '',
        'search_range': '0',
        'content_type': '0',
        'target_account_id': account_id,
    }


async def perform_search(
        client: httpx.AsyncClient,
        config: MultiTestConfig,
        state: MultiRunState,
        stage: int,
        level: int,
        account_id: str,
        account_concurrency: int,
        total_concurrency: int,
) -> RequestRecord:
    """发送一次定向搜索，不重试且不记录响应正文。"""
    sequence, in_flight, account_in_flight = await state.begin_request(account_id)
    started_at = utc_now()
    started = time.perf_counter()
    http_status = None
    try:
        response = await client.post(SEARCH_PATH, json=search_payload(config, account_id))
        http_status = response.status_code
        outcome, app_code, actual_account_id, request_id = classify_response(
            response, account_id
        )
    except httpx.HTTPError as error:
        outcome = 'transport_error'
        app_code = error.__class__.__name__
        actual_account_id = ''
        request_id = ''

    record = RequestRecord(
        stage=stage,
        configured_concurrency=level,
        configured_account_concurrency=account_concurrency,
        configured_total_concurrency=total_concurrency,
        sequence=sequence,
        started_at=started_at,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        http_status=http_status,
        app_code=app_code,
        outcome=outcome,
        target_account_id=account_id,
        actual_account_id=actual_account_id,
        request_id=request_id,
        in_flight=in_flight,
        account_in_flight=account_in_flight,
    )
    await state.finish_request(record)
    return record


def metrics_for(records: list[RequestRecord], elapsed: float) -> dict[str, Any]:
    successes = sum(record.outcome == 'success' for record in records)
    latencies = [record.latency_ms for record in records]
    return {
        'requests': len(records),
        'successes': successes,
        'success_rate': round(successes / len(records), 4) if records else 0.0,
        'actual_rps': round(len(records) / elapsed, 3),
        'peak_in_flight': max((record.in_flight for record in records), default=0),
        'p50_latency_ms': percentile(latencies, 0.50),
        'p95_latency_ms': percentile(latencies, 0.95),
    }


async def run_stage(
        client: httpx.AsyncClient,
        config: MultiTestConfig,
        state: MultiRunState,
        stage: int,
        level: int,
        allocations: dict[str, int],
) -> dict[str, Any]:
    """按账号分配同步节拍工作协程。"""
    started = time.perf_counter()
    first_record = len(state.records)
    period = 1.0 / config.rps_per_worker
    total_concurrency = sum(allocations.values())

    async def worker(account_id: str) -> None:
        while not state.stop_event.is_set():
            request_started = time.perf_counter()
            if request_started - started >= config.stage_seconds:
                return
            await perform_search(
                client,
                config,
                state,
                stage,
                level,
                account_id,
                allocations[account_id],
                total_concurrency,
            )
            remaining = period - (time.perf_counter() - request_started)
            if remaining > 0 and not state.stop_event.is_set():
                try:
                    await asyncio.wait_for(state.stop_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass

    tasks = [
        worker(account_id)
        for account_id, worker_count in allocations.items()
        for _ in range(worker_count)
    ]
    await asyncio.gather(*tasks)
    elapsed = max(time.perf_counter() - started, 0.000001)
    records = state.records[first_record:]
    summary = {
        'stage': stage,
        'configured_concurrency': level,
        'configured_total_concurrency': total_concurrency,
        **metrics_for(records, elapsed),
        'elapsed_seconds': round(elapsed, 3),
        'accounts': {},
    }
    for account_id in config.account_ids:
        account_records = [
            record for record in records if record.target_account_id == account_id
        ]
        account_metrics = metrics_for(account_records, elapsed)
        account_metrics['configured_concurrency'] = allocations[account_id]
        account_metrics['peak_in_flight'] = max(
            (record.account_in_flight for record in account_records), default=0
        )
        summary['accounts'][account_id] = account_metrics
    return summary


async def check_accounts(
        client: httpx.AsyncClient,
        config: MultiTestConfig,
) -> tuple[bool, str, str | None, list[dict[str, Any]]]:
    try:
        accounts = await fetch_accounts(client)
    except (httpx.HTTPError, PreflightError):
        return False, 'account_status_unavailable', None, []
    items = {str(item.get('account_id') or ''): item for item in accounts}
    if set(items) != set(config.account_ids) or len(accounts) != len(config.account_ids):
        return False, 'account_set_changed', None, accounts
    for account_id in config.account_ids:
        if items[account_id].get('status') != 'available':
            return False, 'account_not_available', account_id, accounts
    return True, 'available', None, accounts


async def cooldown(
        client: httpx.AsyncClient,
        config: MultiTestConfig,
        state: MultiRunState,
) -> None:
    """冷却期间只轮询内部账号状态。"""
    deadline = time.monotonic() + config.cooldown_seconds
    while not state.stop_event.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(min(10.0, max(0.0, deadline - time.monotonic())))
        available, reason, account_id, _ = await check_accounts(client, config)
        if not available:
            kind = 'risk' if reason == 'account_not_available' else 'isolation'
            state._stop(
                kind,
                reason,
                state.last_level,
                account_id,
                state.last_allocations.get(account_id) if account_id else None,
            )


async def observe_accounts(
        client: httpx.AsyncClient,
        config: MultiTestConfig,
        state: MultiRunState,
) -> None:
    """停止施压后被动观察全部目标账号。"""
    deadline = time.monotonic() + config.observation_seconds
    while time.monotonic() < deadline:
        available, reason, account_id, accounts = await check_accounts(client, config)
        state.observations.append({
            'observed_at': utc_now(),
            'status': reason,
            'accounts': [
                {
                    'account_id': item.get('account_id'),
                    'status': item.get('status'),
                    'cooldown_until': item.get('cooldown_until'),
                }
                for item in accounts
            ],
        })
        if state.stop_kind is None and not available:
            kind = 'risk' if reason == 'account_not_available' else 'isolation'
            state._stop(
                kind,
                reason,
                state.last_level,
                account_id,
                state.last_allocations.get(account_id) if account_id else None,
            )
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(10.0, remaining))


def stage_failure(
        summary: dict[str, Any],
        allocations: dict[str, int],
) -> tuple[str | None, str | None]:
    if summary['requests'] == 0 or summary['success_rate'] < 0.99:
        return 'stage_success_rate', None
    for account_id, concurrency in allocations.items():
        if concurrency <= 0:
            continue
        account = summary['accounts'][account_id]
        if account['requests'] == 0 or account['success_rate'] < 0.99:
            return 'stage_account_success_rate', account_id
    return None, None


def performance_failure(
        summary: dict[str, Any],
        allocations: dict[str, int],
        baselines: dict[str, float],
) -> str | None:
    for account_id, concurrency in allocations.items():
        if concurrency <= 0:
            continue
        current = summary['accounts'][account_id]['p95_latency_ms']
        baseline = baselines.get(account_id)
        if baseline is None and current is not None:
            baselines[account_id] = current
        elif baseline and current is not None and current > baseline * 3:
            return account_id
    return None


async def validate_accounts_after_stage(
        client: httpx.AsyncClient,
        config: MultiTestConfig,
        state: MultiRunState,
) -> bool:
    available, reason, account_id, _ = await check_accounts(client, config)
    if available:
        return True
    kind = 'risk' if reason == 'account_not_available' else 'isolation'
    state._stop(
        kind,
        reason,
        state.last_level,
        account_id,
        state.last_allocations.get(account_id) if account_id else None,
    )
    return False


async def execute_test(
        config: MultiTestConfig,
        state: MultiRunState,
        client: httpx.AsyncClient | None = None,
) -> int:
    """执行多账号测试，允许通过 MockTransport 注入客户端。"""
    owned_client = client is None
    if client is None:
        max_connections = max(config.levels)
        if config.mode == 'simultaneous' and config.concurrency_scope == 'per-account':
            max_connections *= len(config.account_ids)
        client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.request_timeout,
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
            ),
            follow_redirects=False,
        )

    try:
        await preflight(client, config)
        # 每个账号都必须先通过定向冒烟，确认服务没有切换账号。
        for account_id in config.account_ids:
            smoke = await perform_search(
                client, config, state, 0, 0, account_id, 1, 1
            )
            if smoke.outcome != 'success':
                if state.stop_kind is None:
                    state._stop('degradation', 'smoke_request_failed', 0, account_id, 1)
                await observe_accounts(client, config, state)
                return exit_code_for(state)
        state.clear_error_windows()

        stage_number = 0
        baselines: dict[str, float] = {}
        if config.mode == 'sequential':
            phases = [
                (account_id, level)
                for account_id in config.account_ids
                for level in config.levels
            ]
        else:
            phases = [(None, level) for level in config.levels]

        for phase_index, (sequential_account_id, level) in enumerate(phases):
            if state.stop_event.is_set():
                break
            stage_number += 1
            state.last_level = level
            allocations = allocation_for(
                config, level, stage_number, sequential_account_id
            )
            state.last_allocations = allocations
            summary = await run_stage(
                client, config, state, stage_number, level, allocations
            )
            if sequential_account_id is not None:
                summary['sequential_account_id'] = sequential_account_id
            state.stage_summaries.append(summary)
            if state.stop_event.is_set():
                break

            reason, account_id = stage_failure(summary, allocations)
            if reason is not None:
                account_concurrency = allocations.get(account_id, 0) if account_id else None
                state._stop(
                    'degradation', reason, level, account_id, account_concurrency
                )
                break
            slow_account = performance_failure(summary, allocations, baselines)
            if slow_account is not None:
                state._stop(
                    'degradation',
                    'p95_latency_degradation',
                    level,
                    slow_account,
                    allocations[slow_account],
                )
                break
            if not await validate_accounts_after_stage(client, config, state):
                break

            for account_id, concurrency in allocations.items():
                if concurrency > 0:
                    state.mark_passed(account_id, concurrency)
            if config.mode == 'simultaneous':
                state.safe_level = level
                state.passed_levels.append(level)

            if phase_index != len(phases) - 1:
                await cooldown(client, config, state)

        stop_before_observation = state.stop_kind
        await observe_accounts(client, config, state)
        if stop_before_observation is None and state.stop_kind == 'risk':
            # 延迟风控时撤销受影响账号最后一个安全并发。
            account_id = state.stop_account_id
            if account_id and state.passed_concurrencies[account_id]:
                trigger = state.passed_concurrencies[account_id].pop()
                state.account_results[account_id]['trigger_concurrency'] = trigger
                passed = state.passed_concurrencies[account_id]
                state.account_results[account_id]['safe_concurrency'] = (
                    max(passed) if passed else None
                )
            if config.mode == 'simultaneous' and state.passed_levels:
                state.trigger_level = state.passed_levels.pop()
                state.safe_level = state.passed_levels[-1] if state.passed_levels else None

        if config.mode == 'sequential' and state.stop_kind is None:
            safe_values = [
                result['safe_concurrency']
                for result in state.account_results.values()
            ]
            state.safe_level = min(safe_values) if all(
                value is not None for value in safe_values
            ) else None
        return exit_code_for(state)
    finally:
        if owned_client:
            await client.aclose()


def exit_code_for(state: MultiRunState) -> int:
    if state.stop_kind == 'risk':
        return 2
    if state.stop_kind == 'isolation':
        return 3
    if state.stop_kind == 'degradation':
        return 4
    return 0


def write_reports(
        output_dir: Path,
        config: MultiTestConfig,
        state: MultiRunState,
        exit_code: int,
        started_at: str,
) -> tuple[Path, Path]:
    """写入逐请求明细和脱敏多账号汇总。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / 'requests.csv'
    json_path = output_dir / 'summary.json'
    with csv_path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RequestRecord.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(record) for record in state.records)

    if state.stop_kind == 'risk':
        threshold_conclusion = 'confirmed_risk_boundary'
    elif state.stop_kind == 'degradation':
        threshold_conclusion = 'risk_threshold_not_confirmed'
    elif state.stop_kind is None:
        threshold_conclusion = 'risk_threshold_above_test_maximum'
    else:
        threshold_conclusion = 'inconclusive'

    account_totals = {}
    for account_id in config.account_ids:
        records = [
            record for record in state.records
            if record.target_account_id == account_id and record.stage > 0
        ]
        result = dict(state.account_results[account_id])
        result.update({
            'requests': len(records),
            'successes': sum(record.outcome == 'success' for record in records),
        })
        result['success_rate'] = round(
            result['successes'] / result['requests'], 4
        ) if result['requests'] else 0.0
        account_totals[account_id] = result

    summary = {
        'schema_version': 2,
        'started_at': started_at,
        'finished_at': utc_now(),
        'exit_code': exit_code,
        'confirmed_risk_control': state.stop_kind == 'risk',
        'threshold_conclusion': threshold_conclusion,
        'stop_kind': state.stop_kind,
        'stop_reason': state.stop_reason,
        'stop_account_id': state.stop_account_id,
        'group_safe_level': state.safe_level,
        'group_trigger_level': state.trigger_level,
        'config': {
            'base_url': config.base_url,
            'account_ids': list(config.account_ids),
            'mode': config.mode,
            'concurrency_scope': config.concurrency_scope,
            'query_length': len(config.query),
            'query_sha256': hashlib.sha256(config.query.encode('utf-8')).hexdigest(),
            'levels': list(config.levels),
            'stage_seconds': config.stage_seconds,
            'rps_per_worker': config.rps_per_worker,
            'cooldown_seconds': config.cooldown_seconds,
            'observation_seconds': config.observation_seconds,
            'request_timeout': config.request_timeout,
        },
        'accounts': account_totals,
        'stage_summaries': state.stage_summaries,
        'observations': state.observations,
    }
    with json_path.open('w', encoding='utf-8') as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return csv_path, json_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='多账号关键词搜索并发风控测试')
    parser.add_argument('--base-url', required=True, type=normalize_base_url)
    parser.add_argument('--account-ids', required=True, type=parse_account_ids,
                        help='逗号分隔的目标账号 ID，至少两个')
    parser.add_argument('--query', required=True, help='固定关键词；报告不保存明文')
    parser.add_argument('--mode', choices=('sequential', 'simultaneous'),
                        default='sequential')
    parser.add_argument('--concurrency-scope', choices=('per-account', 'total'),
                        default='per-account')
    parser.add_argument('--levels', type=parse_levels, default=DEFAULT_LEVELS)
    parser.add_argument('--stage-seconds', type=positive_float, default=60.0)
    parser.add_argument('--rps-per-worker', type=positive_float, default=1.0)
    parser.add_argument('--cooldown-seconds', type=non_negative_float, default=120.0)
    parser.add_argument('--observation-seconds', type=non_negative_float, default=300.0)
    parser.add_argument('--request-timeout', type=positive_float, default=45.0)
    parser.add_argument('--output-dir', type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    output_dir = args.output_dir or Path('reports/multi-account-concurrency') / timestamp
    config = MultiTestConfig(
        base_url=args.base_url,
        account_ids=args.account_ids,
        query=args.query,
        mode=args.mode,
        concurrency_scope=args.concurrency_scope,
        levels=args.levels,
        stage_seconds=args.stage_seconds,
        rps_per_worker=args.rps_per_worker,
        cooldown_seconds=args.cooldown_seconds,
        observation_seconds=args.observation_seconds,
        request_timeout=args.request_timeout,
    )
    state = MultiRunState(config.account_ids)
    started_at = utc_now()
    exit_code = 0
    try:
        exit_code = asyncio.run(execute_test(config, state))
    except PreflightError as error:
        state._stop('isolation', error.args[0], None)
        exit_code = 3
    except KeyboardInterrupt:
        state.stop_kind = 'interrupted'
        state.stop_reason = 'keyboard_interrupt'
        exit_code = 130
    except Exception as error:  # pragma: no cover - CLI 最后保护层
        state.stop_kind = 'degradation'
        state.stop_reason = f'unhandled_{error.__class__.__name__}'
        exit_code = 4

    csv_path, json_path = write_reports(
        output_dir, config, state, exit_code, started_at
    )
    print(
        f'测试结束 exit_code={exit_code} group_safe_level={state.safe_level} '
        f'group_trigger_level={state.trigger_level}'
    )
    print(f'请求明细: {csv_path}')
    print(f'汇总报告: {json_path}')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())

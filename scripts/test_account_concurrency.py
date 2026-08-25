# coding=utf-8
"""对独立测试实例中的单个账号执行关键词搜索并发测试。"""

from __future__ import annotations

# 文件名按用途保留 test_ 前缀，但不作为 pytest 测试模块收集。
__test__ = False

import argparse
import asyncio
import csv
import hashlib
import json
import math
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


DEFAULT_LEVELS = (1, 2, 3, 5, 8, 10)
SEARCH_PATH = '/api/v1/douyin/search_videos'
HEALTH_PATH = '/api/health'
ACCOUNTS_PATH = '/api/v1/douyin/auth/accounts'


class PreflightError(RuntimeError):
    """测试实例或账号隔离检查失败。"""


@dataclass(frozen=True)
class TestConfig:
    base_url: str
    account_id: str
    query: str
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
    sequence: int
    started_at: str
    latency_ms: float
    http_status: int | None
    app_code: str
    outcome: str
    account_id: str
    request_id: str
    in_flight: int


@dataclass
class RunState:
    records: list[RequestRecord] = field(default_factory=list)
    stage_summaries: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rolling_errors: deque[bool] = field(default_factory=lambda: deque(maxlen=20))
    in_flight: int = 0
    peak_in_flight: int = 0
    next_sequence: int = 1
    consecutive_errors: int = 0
    stop_kind: str | None = None
    stop_reason: str | None = None
    trigger_level: int | None = None
    safe_level: int | None = None

    async def begin_request(self) -> tuple[int, int]:
        """原子记录请求开始并返回序号和当前在途数。"""
        async with self.lock:
            sequence = self.next_sequence
            self.next_sequence += 1
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            return sequence, self.in_flight

    async def finish_request(self, record: RequestRecord) -> None:
        """记录完成结果并执行统一停止判定。"""
        async with self.lock:
            self.in_flight -= 1
            self.records.append(record)
            ordinary_error = record.outcome in {
                'transport_error',
                'upstream_error',
                'unexpected_response',
            }
            self.rolling_errors.append(ordinary_error)
            if ordinary_error:
                self.consecutive_errors += 1
            else:
                self.consecutive_errors = 0

            if record.outcome == 'risk_control':
                self._stop('risk', record.app_code or 'risk_control', record.configured_concurrency)
            elif record.outcome == 'account_mismatch':
                self._stop('isolation', 'response_account_mismatch', record.configured_concurrency)
            elif self.consecutive_errors >= 3:
                self._stop('degradation', 'three_consecutive_errors', record.configured_concurrency)
            elif len(self.rolling_errors) == 20:
                error_rate = sum(self.rolling_errors) / len(self.rolling_errors)
                if error_rate >= 0.05:
                    self._stop('degradation', 'rolling_error_rate', record.configured_concurrency)

    def _stop(self, kind: str, reason: str, level: int | None) -> None:
        """只保留首个停止原因。"""
        if self.stop_kind is not None:
            return
        self.stop_kind = kind
        self.stop_reason = reason
        self.trigger_level = level
        self.stop_event.set()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def percentile(values: list[float], ratio: float) -> float | None:
    """使用最近秩计算百分位，兼容单个样本。"""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * ratio) - 1)
    return round(ordered[index], 2)


def parse_levels(raw_value: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in raw_value.split(',') if item.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError('并发阶梯必须是逗号分隔的正整数') from error
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError('并发阶梯必须是逗号分隔的正整数')
    if tuple(sorted(set(values))) != values:
        raise argparse.ArgumentTypeError('并发阶梯必须严格递增且不能重复')
    return values


def _envelope_data(response: httpx.Response, label: str) -> dict[str, Any]:
    """读取内部 API 包装结构，不在异常中携带响应正文。"""
    if response.status_code != 200:
        raise PreflightError(f'{label}返回 HTTP {response.status_code}')
    try:
        body = response.json()
    except ValueError as error:
        raise PreflightError(f'{label}未返回有效 JSON') from error
    data = body.get('data') if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get('success') is not True or not isinstance(data, dict):
        raise PreflightError(f'{label}响应格式无效')
    return data


async def fetch_accounts(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await client.get(ACCOUNTS_PATH)
    data = _envelope_data(response, '账号列表')
    items = data.get('items')
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise PreflightError('账号列表响应格式无效')
    return items


async def preflight(client: httpx.AsyncClient, config: TestConfig) -> None:
    """确认服务并发配置与单账号隔离条件。"""
    try:
        health = _envelope_data(await client.get(HEALTH_PATH), '健康检查')
    except httpx.HTTPError as error:
        raise PreflightError('健康检查请求失败') from error
    if health.get('status') != 'ok':
        raise PreflightError('服务状态不是 ok')
    required = max(config.levels)
    global_limit = health.get('max_concurrent_requests')
    account_limit = health.get('max_concurrent_requests_per_account')
    if not isinstance(global_limit, int) or global_limit < required:
        raise PreflightError(f'全局并发上限低于测试上限 {required}')
    if not isinstance(account_limit, int) or account_limit < required:
        raise PreflightError(f'单账号并发上限低于测试上限 {required}')

    try:
        accounts = await fetch_accounts(client)
    except httpx.HTTPError as error:
        raise PreflightError('账号列表请求失败') from error
    if len(accounts) != 1:
        raise PreflightError('独立测试实例必须恰好包含一个账号')
    account = accounts[0]
    if account.get('account_id') != config.account_id:
        raise PreflightError('唯一账号与 --account-id 不一致')
    if account.get('status') != 'available':
        raise PreflightError('目标账号当前不可用')


def _safe_signal(body: Any) -> str:
    """只接受服务端定义的稳定风控信号。"""
    allowed = {'http_429', 'verification_redirect', 'business_risk_signal'}
    if not isinstance(body, dict):
        return 'api_risk_signal'
    error = body.get('error')
    details = error.get('details') if isinstance(error, dict) else None
    signal = details.get('signal') if isinstance(details, dict) else None
    return signal if signal in allowed else 'api_risk_signal'


def classify_response(
        response: httpx.Response,
        expected_account_id: str,
) -> tuple[str, str, str, str]:
    """返回 outcome、应用码、账号和请求 ID。"""
    request_id = response.headers.get('X-Request-ID', '')
    try:
        body = response.json()
    except ValueError:
        body = None

    error = body.get('error') if isinstance(body, dict) else None
    app_code = error.get('code', '') if isinstance(error, dict) else ''
    if response.status_code == 429 or app_code == 'UPSTREAM_RISK_CONTROL':
        return 'risk_control', _safe_signal(body), '', request_id
    if response.status_code == 403:
        return 'risk_control', 'http_403', '', request_id
    if response.status_code == 503 and app_code == 'NO_AVAILABLE_ACCOUNT':
        return 'risk_control', 'account_unavailable', '', request_id
    if response.status_code == 502:
        return 'upstream_error', app_code or 'UPSTREAM_ERROR', '', request_id

    data = body.get('data') if isinstance(body, dict) else None
    if (
            response.status_code == 200
            and isinstance(body, dict)
            and body.get('success') is True
            and isinstance(data, dict)
    ):
        account_id = str(data.get('account_id') or '')
        if account_id != expected_account_id:
            return 'account_mismatch', 'ACCOUNT_MISMATCH', account_id, request_id
        return 'success', '', account_id, request_id
    return 'unexpected_response', app_code or f'HTTP_{response.status_code}', '', request_id


async def perform_search(
        client: httpx.AsyncClient,
        config: TestConfig,
        state: RunState,
        stage: int,
        concurrency: int,
) -> RequestRecord:
    """发送一次无重试搜索并记录脱敏结果。"""
    sequence, in_flight = await state.begin_request()
    started_at = utc_now()
    started = time.perf_counter()
    http_status = None
    try:
        response = await client.post(SEARCH_PATH, json={
            'query': config.query,
            'limit': 1,
            'sort_type': '0',
            'publish_time': '0',
            'filter_duration': '',
            'search_range': '0',
            'content_type': '0',
        })
        http_status = response.status_code
        outcome, app_code, account_id, request_id = classify_response(
            response,
            config.account_id,
        )
    except httpx.HTTPError as error:
        # 不记录异常正文，避免请求内容进入报告。
        outcome = 'transport_error'
        app_code = error.__class__.__name__
        account_id = ''
        request_id = ''

    record = RequestRecord(
        stage=stage,
        configured_concurrency=concurrency,
        sequence=sequence,
        started_at=started_at,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        http_status=http_status,
        app_code=app_code,
        outcome=outcome,
        account_id=account_id,
        request_id=request_id,
        in_flight=in_flight,
    )
    await state.finish_request(record)
    return record


async def run_stage(
        client: httpx.AsyncClient,
        config: TestConfig,
        state: RunState,
        stage: int,
        concurrency: int,
) -> dict[str, Any]:
    """按同步节拍执行一个并发阶段。"""
    started = time.perf_counter()
    first_record = len(state.records)
    period = 1.0 / config.rps_per_worker

    async def worker() -> None:
        while not state.stop_event.is_set():
            request_started = time.perf_counter()
            if request_started - started >= config.stage_seconds:
                return
            await perform_search(client, config, state, stage, concurrency)
            remaining = period - (time.perf_counter() - request_started)
            if remaining > 0 and not state.stop_event.is_set():
                try:
                    await asyncio.wait_for(state.stop_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = max(time.perf_counter() - started, 0.000001)
    records = state.records[first_record:]
    successes = sum(record.outcome == 'success' for record in records)
    latencies = [record.latency_ms for record in records]
    return {
        'stage': stage,
        'concurrency': concurrency,
        'requests': len(records),
        'successes': successes,
        'success_rate': round(successes / len(records), 4) if records else 0.0,
        'actual_rps': round(len(records) / elapsed, 3),
        'peak_in_flight': max((record.in_flight for record in records), default=0),
        'p50_latency_ms': percentile(latencies, 0.50),
        'p95_latency_ms': percentile(latencies, 0.95),
        'elapsed_seconds': round(elapsed, 3),
    }


async def verify_account_available(client: httpx.AsyncClient, config: TestConfig) -> tuple[bool, str]:
    """阶段间再次确认唯一账号未发生变化。"""
    try:
        accounts = await fetch_accounts(client)
    except (httpx.HTTPError, PreflightError):
        return False, 'account_status_unavailable'
    if len(accounts) != 1 or accounts[0].get('account_id') != config.account_id:
        return False, 'account_isolation_changed'
    if accounts[0].get('status') != 'available':
        return False, 'account_not_available'
    return True, 'available'


async def cooldown(
        client: httpx.AsyncClient,
        config: TestConfig,
        state: RunState,
        concurrency: int,
) -> None:
    """冷却期间只轮询内部账号状态，不访问抖音。"""
    deadline = time.monotonic() + config.cooldown_seconds
    while not state.stop_event.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(min(10.0, max(0.0, deadline - time.monotonic())))
        available, reason = await verify_account_available(client, config)
        if not available:
            kind = 'risk' if reason == 'account_not_available' else 'isolation'
            state._stop(kind, reason, concurrency)


async def observe_account(client: httpx.AsyncClient, config: TestConfig, state: RunState) -> None:
    """停止施压后被动记录账号状态。"""
    if config.observation_seconds <= 0:
        return
    deadline = time.monotonic() + config.observation_seconds
    while time.monotonic() < deadline:
        try:
            accounts = await fetch_accounts(client)
            item = accounts[0] if len(accounts) == 1 else None
            state.observations.append({
                'observed_at': utc_now(),
                'account_count': len(accounts),
                'account_id': item.get('account_id') if item else None,
                'status': item.get('status') if item else 'isolation_changed',
                'cooldown_until': item.get('cooldown_until') if item else None,
            })
            if state.stop_kind is None:
                if item is None or item.get('account_id') != config.account_id:
                    state._stop('isolation', 'account_isolation_changed_during_observation', state.safe_level)
                elif item.get('status') != 'available':
                    state._stop('risk', 'account_not_available_during_observation', state.safe_level)
        except (httpx.HTTPError, PreflightError):
            state.observations.append({
                'observed_at': utc_now(),
                'status': 'status_unavailable',
            })
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(10.0, remaining))


def exit_code_for(state: RunState) -> int:
    if state.stop_kind == 'risk':
        return 2
    if state.stop_kind == 'isolation':
        return 3
    if state.stop_kind == 'degradation':
        return 4
    return 0


async def execute_test(
        config: TestConfig,
        state: RunState,
        client: httpx.AsyncClient | None = None,
) -> int:
    """执行完整测试；允许测试代码注入 MockTransport 客户端。"""
    owned_client = client is None
    if client is None:
        limits = httpx.Limits(
            max_connections=max(config.levels),
            max_keepalive_connections=max(config.levels),
        )
        client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.request_timeout,
            limits=limits,
            follow_redirects=False,
        )
    try:
        await preflight(client, config)
        smoke = await perform_search(client, config, state, 0, 1)
        if smoke.outcome != 'success':
            if state.stop_kind is None:
                state._stop('degradation', 'smoke_request_failed', 0)
            await observe_account(client, config, state)
            return exit_code_for(state)

        # 冒烟请求不参与阶段错误率和连续错误统计。
        state.rolling_errors.clear()
        state.consecutive_errors = 0
        baseline_p95 = None
        for stage, concurrency in enumerate(config.levels, start=1):
            summary = await run_stage(client, config, state, stage, concurrency)
            state.stage_summaries.append(summary)
            if state.stop_event.is_set():
                break
            if summary['requests'] == 0 or summary['success_rate'] < 0.99:
                state._stop('degradation', 'stage_success_rate', concurrency)
                break
            if concurrency == config.levels[0]:
                baseline_p95 = summary['p95_latency_ms']
            elif baseline_p95 and summary['p95_latency_ms'] > baseline_p95 * 3:
                state._stop('degradation', 'p95_latency_degradation', concurrency)
                break

            available, reason = await verify_account_available(client, config)
            if not available:
                kind = 'risk' if reason == 'account_not_available' else 'isolation'
                state._stop(kind, reason, concurrency)
                break
            if concurrency != config.levels[-1]:
                await cooldown(client, config, state, concurrency)
                if state.stop_event.is_set():
                    break
            state.safe_level = concurrency

        stop_before_observation = state.stop_kind
        await observe_account(client, config, state)
        if stop_before_observation is None and state.stop_kind in {'risk', 'isolation'}:
            # 观察期出现延迟异常时，当前触发级不能继续作为安全级。
            trigger_level = state.trigger_level
            if trigger_level in config.levels:
                index = config.levels.index(trigger_level)
                state.safe_level = config.levels[index - 1] if index > 0 else None
        return exit_code_for(state)
    finally:
        if owned_client:
            await client.aclose()


def write_reports(
        output_dir: Path,
        config: TestConfig,
        state: RunState,
        exit_code: int,
        started_at: str,
) -> tuple[Path, Path]:
    """生成逐请求 CSV 和脱敏汇总 JSON。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / 'requests.csv'
    json_path = output_dir / 'summary.json'
    fieldnames = list(RequestRecord.__dataclass_fields__)
    with csv_path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
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

    summary = {
        'schema_version': 1,
        'started_at': started_at,
        'finished_at': utc_now(),
        'exit_code': exit_code,
        'confirmed_risk_control': state.stop_kind == 'risk',
        'threshold_conclusion': threshold_conclusion,
        'stop_kind': state.stop_kind,
        'stop_reason': state.stop_reason,
        'safe_level': state.safe_level,
        'trigger_level': state.trigger_level,
        'config': {
            'base_url': config.base_url,
            'account_id': config.account_id,
            'query_length': len(config.query),
            'query_sha256': hashlib.sha256(config.query.encode('utf-8')).hexdigest(),
            'levels': list(config.levels),
            'stage_seconds': config.stage_seconds,
            'rps_per_worker': config.rps_per_worker,
            'cooldown_seconds': config.cooldown_seconds,
            'observation_seconds': config.observation_seconds,
            'request_timeout': config.request_timeout,
        },
        'stage_summaries': state.stage_summaries,
        'observations': state.observations,
    }
    with json_path.open('w', encoding='utf-8') as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return csv_path, json_path


def positive_float(raw_value: str) -> float:
    try:
        value = float(raw_value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('必须是正数') from error
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('必须是正数')
    return value


def non_negative_float(raw_value: str) -> float:
    try:
        value = float(raw_value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('必须是非负数') from error
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError('必须是非负数')
    return value


def normalize_base_url(raw_value: str) -> str:
    """限制为不含凭证和查询参数的 HTTP(S) 服务地址。"""
    parsed = urlsplit(raw_value.strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise argparse.ArgumentTypeError('服务地址必须是有效的 HTTP(S) URL')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError('服务地址不能包含凭证、查询参数或片段')
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', ''))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='单账号关键词搜索并发风控测试')
    parser.add_argument('--base-url', required=True, type=normalize_base_url,
                        help='独立 FastAPI 测试实例地址')
    parser.add_argument('--account-id', required=True, help='唯一测试账号 ID')
    parser.add_argument('--query', required=True, help='固定测试关键词；报告不保存明文')
    parser.add_argument('--levels', type=parse_levels, default=DEFAULT_LEVELS,
                        help='并发阶梯，默认 1,2,3,5,8,10')
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
    output_dir = args.output_dir or Path('reports/account-concurrency') / timestamp
    config = TestConfig(
        base_url=args.base_url,
        account_id=args.account_id,
        query=args.query,
        levels=args.levels,
        stage_seconds=args.stage_seconds,
        rps_per_worker=args.rps_per_worker,
        cooldown_seconds=args.cooldown_seconds,
        observation_seconds=args.observation_seconds,
        request_timeout=args.request_timeout,
    )
    state = RunState()
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

    csv_path, json_path = write_reports(output_dir, config, state, exit_code, started_at)
    print(f'测试结束 exit_code={exit_code} safe_level={state.safe_level} '
          f'trigger_level={state.trigger_level}')
    print(f'请求明细: {csv_path}')
    print(f'汇总报告: {json_path}')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())

# coding=utf-8
import asyncio
import json

import httpx
import pytest

import scripts.test_account_concurrency as concurrency_script
from scripts.test_account_concurrency import (
    PreflightError,
    RequestRecord,
    RunState,
    TestConfig as ConcurrencyTestConfig,
    execute_test,
    write_reports,
)


def envelope(data, status_code=200):
    return httpx.Response(status_code, json={
        'success': status_code < 400,
        'request_id': 'request-id',
        'data': data,
    }, headers={'X-Request-ID': 'request-id'})


def error_response(status_code, code, signal=None):
    error = {'code': code, 'message': 'safe message'}
    if signal:
        error['details'] = {'signal': signal}
    return httpx.Response(status_code, json={
        'success': False,
        'request_id': 'request-id',
        'error': error,
    }, headers={'X-Request-ID': 'request-id'})


def make_config(**overrides):
    values = {
        'base_url': 'http://testserver',
        'account_id': 'test-account',
        'query': 'confidential-keyword',
        'levels': (1,),
        'stage_seconds': 0.03,
        'rps_per_worker': 200.0,
        'cooldown_seconds': 0.0,
        'observation_seconds': 0.0,
        'request_timeout': 1.0,
    }
    values.update(overrides)
    return ConcurrencyTestConfig(**values)


def make_client(handler):
    return httpx.AsyncClient(
        base_url='http://testserver',
        transport=httpx.MockTransport(handler),
    )


def standard_handler(search_response=None, accounts=None, limit=10):
    account_items = accounts or [{
        'account_id': 'test-account',
        'status': 'available',
        'cooldown_until': None,
    }]

    def handler(request):
        if request.url.path == '/api/health':
            return envelope({
                'status': 'ok',
                'max_concurrent_requests': limit,
                'max_concurrent_requests_per_account': limit,
            })
        if request.url.path.endswith('/auth/accounts'):
            return envelope({'items': account_items})
        if search_response is not None:
            return search_response(request) if callable(search_response) else search_response
        return envelope({'account_id': 'test-account', 'items': []})

    return handler


@pytest.mark.parametrize('accounts,limit', [
    ([{'account_id': 'test-account', 'status': 'available'},
      {'account_id': 'other', 'status': 'available'}], 10),
    ([{'account_id': 'other', 'status': 'available'}], 10),
    ([{'account_id': 'test-account', 'status': 'available'}], 1),
])
def test_preflight_rejects_non_isolated_or_underconfigured_instance(accounts, limit):
    config = make_config(levels=(1, 2))
    state = RunState()

    async def run():
        async with make_client(standard_handler(accounts=accounts, limit=limit)) as client:
            with pytest.raises(PreflightError):
                await execute_test(config, state, client)

    asyncio.run(run())
    assert not any(record.stage >= 0 for record in state.records)


def test_successful_run_calculates_safe_level():
    config = make_config()
    state = RunState()

    async def run():
        async with make_client(standard_handler()) as client:
            return await execute_test(config, state, client)

    exit_code = asyncio.run(run())
    assert exit_code == 0
    assert state.safe_level == 1
    assert state.trigger_level is None
    assert state.stage_summaries[0]['success_rate'] == 1.0
    assert state.stage_summaries[0]['peak_in_flight'] == 1


def test_first_risk_response_stops_run_immediately():
    calls = 0

    def search_response(_request):
        nonlocal calls
        calls += 1
        if calls >= 3:
            return error_response(429, 'UPSTREAM_RISK_CONTROL', 'http_429')
        return envelope({'account_id': 'test-account', 'items': []})

    config = make_config(stage_seconds=0.2)
    state = RunState()

    async def run():
        async with make_client(standard_handler(search_response=search_response)) as client:
            return await execute_test(config, state, client)

    exit_code = asyncio.run(run())
    assert exit_code == 2
    assert state.stop_kind == 'risk'
    assert state.stop_reason == 'http_429'
    assert state.trigger_level == 1
    assert calls == 3


def test_response_account_mismatch_is_an_isolation_failure():
    config = make_config()
    state = RunState()
    response = envelope({'account_id': 'other-account', 'items': []})

    async def run():
        async with make_client(standard_handler(search_response=response)) as client:
            return await execute_test(config, state, client)

    exit_code = asyncio.run(run())
    assert exit_code == 3
    assert state.stop_kind == 'isolation'
    assert state.stop_reason == 'response_account_mismatch'


def test_non_json_search_response_is_counted_as_degradation():
    calls = 0

    def search_response(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return envelope({'account_id': 'test-account', 'items': []})
        return httpx.Response(200, text='<html>temporary page</html>')

    config = make_config(stage_seconds=0.2)
    state = RunState()

    async def run():
        async with make_client(standard_handler(search_response=search_response)) as client:
            return await execute_test(config, state, client)

    exit_code = asyncio.run(run())
    assert exit_code == 4
    assert state.stop_reason == 'three_consecutive_errors'


def test_three_consecutive_upstream_errors_stop_as_degradation():
    calls = 0

    def search_response(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return envelope({'account_id': 'test-account', 'items': []})
        return error_response(502, 'UPSTREAM_ERROR')

    config = make_config(stage_seconds=0.2)
    state = RunState()

    async def run():
        async with make_client(standard_handler(search_response=search_response)) as client:
            return await execute_test(config, state, client)

    exit_code = asyncio.run(run())
    assert exit_code == 4
    assert state.stop_reason == 'three_consecutive_errors'
    assert calls == 4


def test_rolling_error_rate_stops_after_twenty_stage_results():
    calls = 0

    def search_response(_request):
        nonlocal calls
        calls += 1
        # 第一次是冒烟请求，阶段中只注入一次非连续错误。
        if calls == 5:
            return error_response(502, 'UPSTREAM_ERROR')
        return envelope({'account_id': 'test-account', 'items': []})

    config = make_config(stage_seconds=0.5, rps_per_worker=500.0)
    state = RunState()

    async def run():
        async with make_client(standard_handler(search_response=search_response)) as client:
            return await execute_test(config, state, client)

    exit_code = asyncio.run(run())
    assert exit_code == 4
    assert state.stop_reason == 'rolling_error_rate'
    assert len([record for record in state.records if record.stage == 1]) == 20


def test_account_cooling_between_stages_is_a_risk_signal():
    account_calls = 0

    def handler(request):
        nonlocal account_calls
        if request.url.path == '/api/health':
            return envelope({
                'status': 'ok',
                'max_concurrent_requests': 10,
                'max_concurrent_requests_per_account': 10,
            })
        if request.url.path.endswith('/auth/accounts'):
            account_calls += 1
            status = 'available' if account_calls == 1 else 'cooling'
            return envelope({'items': [{
                'account_id': 'test-account',
                'status': status,
                'cooldown_until': '2026-08-18T01:00:00Z' if status == 'cooling' else None,
            }]})
        return envelope({'account_id': 'test-account', 'items': []})

    config = make_config()
    state = RunState()

    async def run():
        async with make_client(handler) as client:
            return await execute_test(config, state, client)

    exit_code = asyncio.run(run())
    assert exit_code == 2
    assert state.stop_reason == 'account_not_available'
    assert state.safe_level is None


def test_delayed_cooling_during_observation_revises_safe_level():
    account_calls = 0

    def handler(request):
        nonlocal account_calls
        if request.url.path == '/api/health':
            return envelope({
                'status': 'ok',
                'max_concurrent_requests': 10,
                'max_concurrent_requests_per_account': 10,
            })
        if request.url.path.endswith('/auth/accounts'):
            account_calls += 1
            status = 'cooling' if account_calls >= 3 else 'available'
            return envelope({'items': [{
                'account_id': 'test-account',
                'status': status,
                'cooldown_until': None,
            }]})
        return envelope({'account_id': 'test-account', 'items': []})

    config = make_config(observation_seconds=0.01)
    state = RunState()

    async def run():
        async with make_client(handler) as client:
            return await execute_test(config, state, client)

    exit_code = asyncio.run(run())
    assert exit_code == 2
    assert state.trigger_level == 1
    assert state.safe_level is None
    assert state.stop_reason == 'account_not_available_during_observation'


def test_reports_redact_query_and_do_not_store_response_body(tmp_path):
    config = make_config(query='secret-query-value')
    state = RunState(
        records=[RequestRecord(
            stage=1,
            configured_concurrency=1,
            sequence=1,
            started_at='2026-08-18T00:00:00Z',
            latency_ms=10.0,
            http_status=429,
            app_code='http_429',
            outcome='risk_control',
            account_id='',
            request_id='request-id',
            in_flight=1,
        )],
        stop_kind='risk',
        stop_reason='http_429',
        trigger_level=1,
    )

    csv_path, json_path = write_reports(
        tmp_path,
        config,
        state,
        2,
        '2026-08-18T00:00:00Z',
    )
    combined = csv_path.read_text(encoding='utf-8-sig') + json_path.read_text(encoding='utf-8')
    summary = json.loads(json_path.read_text(encoding='utf-8'))

    assert config.query not in combined
    assert 'private-upstream-response' not in combined
    assert summary['config']['query_length'] == len(config.query)
    assert len(summary['config']['query_sha256']) == 64
    assert summary['threshold_conclusion'] == 'confirmed_risk_boundary'


def test_keyboard_interrupt_writes_partial_report(tmp_path, monkeypatch):
    async def interrupt(_config, _state):
        raise KeyboardInterrupt

    monkeypatch.setattr(concurrency_script, 'execute_test', interrupt)
    exit_code = concurrency_script.main([
        '--base-url', 'http://testserver',
        '--account-id', 'test-account',
        '--query', 'secret-interrupted-query',
        '--output-dir', str(tmp_path),
    ])

    summary = json.loads((tmp_path / 'summary.json').read_text(encoding='utf-8'))
    assert exit_code == 130
    assert summary['exit_code'] == 130
    assert summary['stop_kind'] == 'interrupted'
    assert 'secret-interrupted-query' not in (tmp_path / 'summary.json').read_text(encoding='utf-8')

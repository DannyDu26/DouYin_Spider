# coding=utf-8
import asyncio
import json

import httpx
import pytest

import scripts.test_multi_account_concurrency as multi_script
from scripts.test_account_concurrency import PreflightError
from scripts.test_multi_account_concurrency import (
    MultiRunState,
    MultiTestConfig,
    RequestRecord,
    execute_test,
    write_reports,
)


ACCOUNT_IDS = ('account-a', 'account-b')


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
        'account_ids': ACCOUNT_IDS,
        'query': 'confidential-keyword',
        'mode': 'sequential',
        'concurrency_scope': 'per-account',
        'levels': (1,),
        'stage_seconds': 0.03,
        'rps_per_worker': 200.0,
        'cooldown_seconds': 0.0,
        'observation_seconds': 0.0,
        'request_timeout': 1.0,
    }
    values.update(overrides)
    return MultiTestConfig(**values)


def make_client(handler):
    return httpx.AsyncClient(
        base_url='http://testserver',
        transport=httpx.MockTransport(handler),
    )


def standard_handler(
        search_response=None,
        accounts=None,
        global_limit=20,
        account_limit=10,
        pinning=True,
):
    items = accounts or [
        {'account_id': account_id, 'status': 'available', 'cooldown_until': None}
        for account_id in ACCOUNT_IDS
    ]

    def handler(request):
        if request.url.path == '/api/health':
            return envelope({
                'status': 'ok',
                'max_concurrent_requests': global_limit,
                'max_concurrent_requests_per_account': account_limit,
                'test_account_pinning_enabled': pinning,
            })
        if request.url.path.endswith('/auth/accounts'):
            return envelope({'items': items})
        payload = json.loads(request.content.decode('utf-8'))
        if search_response is not None:
            return search_response(request, payload)
        return envelope({'account_id': payload['target_account_id'], 'items': []})

    return handler


@pytest.mark.parametrize('handler', [
    standard_handler(pinning=False),
    standard_handler(accounts=[{
        'account_id': 'account-a', 'status': 'available', 'cooldown_until': None,
    }]),
    standard_handler(global_limit=1),
    standard_handler(account_limit=1),
])
def test_preflight_rejects_unsafe_instance(handler):
    config = make_config(levels=(1, 2))
    state = MultiRunState(config.account_ids)

    async def run():
        async with make_client(handler) as client:
            with pytest.raises(PreflightError):
                await execute_test(config, state, client)

    asyncio.run(run())
    assert state.records == []


def test_sequential_run_calculates_each_account_safe_concurrency():
    config = make_config()
    state = MultiRunState(config.account_ids)

    async def run():
        async with make_client(standard_handler()) as client:
            return await execute_test(config, state, client)

    exit_code = asyncio.run(run())

    assert exit_code == 0
    assert state.safe_level == 1
    assert len(state.stage_summaries) == 2
    assert state.account_results['account-a']['safe_concurrency'] == 1
    assert state.account_results['account-b']['safe_concurrency'] == 1
    assert all(record.target_account_id == record.actual_account_id for record in state.records)


def test_simultaneous_per_account_scope_multiplies_global_concurrency():
    config = make_config(mode='simultaneous')
    state = MultiRunState(config.account_ids)

    async def run():
        async with make_client(standard_handler()) as client:
            return await execute_test(config, state, client)

    assert asyncio.run(run()) == 0
    summary = state.stage_summaries[0]
    assert summary['configured_concurrency'] == 1
    assert summary['configured_total_concurrency'] == 2
    assert summary['accounts']['account-a']['configured_concurrency'] == 1
    assert summary['accounts']['account-b']['configured_concurrency'] == 1


def test_simultaneous_total_scope_distributes_fixed_global_concurrency():
    config = make_config(
        mode='simultaneous',
        concurrency_scope='total',
        levels=(3,),
    )
    state = MultiRunState(config.account_ids)

    async def run():
        async with make_client(standard_handler(global_limit=3, account_limit=2)) as client:
            return await execute_test(config, state, client)

    assert asyncio.run(run()) == 0
    summary = state.stage_summaries[0]
    allocations = {
        account_id: metrics['configured_concurrency']
        for account_id, metrics in summary['accounts'].items()
    }
    assert summary['configured_total_concurrency'] == 3
    assert allocations == {'account-a': 2, 'account-b': 1}


def test_first_account_risk_stops_all_accounts():
    calls = {account_id: 0 for account_id in ACCOUNT_IDS}

    def search_response(_request, payload):
        account_id = payload['target_account_id']
        calls[account_id] += 1
        # 每个账号第一次调用为冒烟，账号 B 的首个阶段请求触发风控。
        if account_id == 'account-b' and calls[account_id] == 2:
            return error_response(429, 'UPSTREAM_RISK_CONTROL', 'http_429')
        return envelope({'account_id': account_id, 'items': []})

    config = make_config(mode='simultaneous', stage_seconds=0.2)
    state = MultiRunState(config.account_ids)

    async def run():
        async with make_client(standard_handler(search_response=search_response)) as client:
            return await execute_test(config, state, client)

    assert asyncio.run(run()) == 2
    assert state.stop_kind == 'risk'
    assert state.stop_account_id == 'account-b'
    assert state.trigger_level == 1
    assert state.account_results['account-b']['trigger_concurrency'] == 1


def test_account_mismatch_is_an_isolation_failure():
    def search_response(_request, payload):
        actual = 'account-b' if payload['target_account_id'] == 'account-a' else 'account-a'
        return envelope({'account_id': actual, 'items': []})

    config = make_config()
    state = MultiRunState(config.account_ids)

    async def run():
        async with make_client(standard_handler(search_response=search_response)) as client:
            return await execute_test(config, state, client)

    assert asyncio.run(run()) == 3
    assert state.stop_kind == 'isolation'
    assert state.stop_reason == 'ACCOUNT_MISMATCH'


def test_three_errors_are_tracked_per_account():
    calls = {account_id: 0 for account_id in ACCOUNT_IDS}

    def search_response(_request, payload):
        account_id = payload['target_account_id']
        calls[account_id] += 1
        if account_id == 'account-a' and calls[account_id] > 1:
            return error_response(502, 'UPSTREAM_ERROR')
        return envelope({'account_id': account_id, 'items': []})

    config = make_config(stage_seconds=0.2)
    state = MultiRunState(config.account_ids)

    async def run():
        async with make_client(standard_handler(search_response=search_response)) as client:
            return await execute_test(config, state, client)

    assert asyncio.run(run()) == 4
    assert state.stop_kind == 'degradation'
    assert state.stop_reason == 'three_consecutive_errors'
    assert state.stop_account_id == 'account-a'


def test_reports_are_redacted_and_include_account_dimensions(tmp_path):
    config = make_config(query='secret-query-value')
    state = MultiRunState(config.account_ids)
    state.records.append(RequestRecord(
        stage=1,
        configured_concurrency=1,
        configured_account_concurrency=1,
        configured_total_concurrency=2,
        sequence=1,
        started_at='2026-08-19T00:00:00Z',
        latency_ms=10.0,
        http_status=429,
        app_code='http_429',
        outcome='risk_control',
        target_account_id='account-a',
        actual_account_id='account-a',
        request_id='request-id',
        in_flight=1,
        account_in_flight=1,
    ))
    state._stop('risk', 'http_429', 1, 'account-a', 1)

    csv_path, json_path = write_reports(
        tmp_path, config, state, 2, '2026-08-19T00:00:00Z'
    )
    combined = (
        csv_path.read_text(encoding='utf-8-sig')
        + json_path.read_text(encoding='utf-8')
    )
    summary = json.loads(json_path.read_text(encoding='utf-8'))

    assert config.query not in combined
    assert 'cookie' not in combined.lower()
    assert summary['schema_version'] == 2
    assert summary['stop_account_id'] == 'account-a'
    assert len(summary['config']['query_sha256']) == 64


def test_keyboard_interrupt_writes_partial_report(tmp_path, monkeypatch):
    async def interrupt(_config, _state):
        raise KeyboardInterrupt

    monkeypatch.setattr(multi_script, 'execute_test', interrupt)
    exit_code = multi_script.main([
        '--base-url', 'http://testserver',
        '--account-ids', 'account-a,account-b',
        '--query', 'secret-interrupted-query',
        '--output-dir', str(tmp_path),
    ])

    summary_text = (tmp_path / 'summary.json').read_text(encoding='utf-8')
    summary = json.loads(summary_text)
    assert exit_code == 130
    assert summary['stop_kind'] == 'interrupted'
    assert 'secret-interrupted-query' not in summary_text

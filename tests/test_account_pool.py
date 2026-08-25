# coding=utf-8
import asyncio
from datetime import datetime, timezone
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import main
from app.account_pool import AccountPool, NoAvailableAccountError
from app.account_store import (
    CredentialRecord,
    CredentialStoreError,
    MySQLCredentialStore,
    serialize_credential,
)
from builder.auth import DouyinAuth
from dy_apis.douyin_api import DouyinAuthenticationError
from app.spider_service import SpiderService, UpstreamServiceError


def make_auth(label: str) -> DouyinAuth:
    auth = DouyinAuth()
    auth.perepare_auth(
        f'sessionid={label}; s_v_web_id=fp-{label}; ttwid=tw-{label}',
        '',
        '',
    )
    auth.ticket = f'ticket-{label}'
    auth.ts_sign = f'sign-{label}'
    auth.client_cert = f'cert-{label}'
    auth.private_key = f'key-{label}'
    auth.label = label
    return auth


def make_record(row_id: int, account_id: str, auth=None, invalid_reason=None):
    return CredentialRecord(
        row_id=row_id,
        account_id=account_id,
        created_at=datetime.now(timezone.utc),
        auth=auth,
        invalid_reason=invalid_reason,
    )


TEST_PROJECT_ID = 22


def make_store() -> MySQLCredentialStore:
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    store = MySQLCredentialStore(engine, project_id=TEST_PROJECT_ID)
    store.table.create(engine)
    return store


def make_work(work_id='100'):
    return {
        'aweme_id': work_id,
        'aweme_type': 0,
        'desc': f'作品 {work_id}',
        'create_time': 1_700_000_000,
        'author': {
            'sec_uid': 'sec-user',
            'unique_id': 'user-id',
            'nickname': '账号池测试',
            'avatar_thumb': {'url_list': ['https://example.com/avatar.jpg']},
        },
        'statistics': {
            'digg_count': 1,
            'comment_count': 2,
            'collect_count': 3,
            'share_count': 4,
        },
        'video': {
            'cover': {'url_list': ['https://example.com/cover.jpg']},
            'play_addr': {'url_list': ['https://example.com/video.mp4']},
        },
        'images': [],
        'text_extra': [],
    }


def test_store_updates_same_type_and_account_instead_of_inserting():
    store = make_store()
    first = store.insert('account-a', make_auth('old'))
    latest = store.insert('account-a', make_auth('new'))
    with store.engine.begin() as connection:
        connection.execute(store.table.insert().values(
            project_id=TEST_PROJECT_ID,
            type='another_crawler',
            account_id='other',
            cookie='plain-cookie',
            remark='other',
        ))

    records = store.load_latest()
    assert len(records) == 1
    assert records[0].row_id == latest.row_id
    assert records[0].row_id == first.row_id
    assert records[0].auth.cookie['sessionid'] == 'new'
    with store.engine.connect() as connection:
        stored_rows = connection.execute(
            store.table.select().where(store.table.c.type == store.credential_type)
        ).mappings().all()
    assert len(stored_rows) == 1
    stored = stored_rows[0]
    assert stored['account_id'] == 'account-a'
    assert stored['project_id'] == 22
    assert stored['remark'] is None
    assert stored['cookie'] == 'sessionid=new; s_v_web_id=fp-new; ttwid=tw-new'
    assert not stored['cookie'].lstrip().startswith('{')


def test_store_update_is_isolated_by_project_type_and_account():
    """更新 Cookie 时不能影响其他项目、类型或账号。"""
    store = make_store()
    target = store.insert('account-a', make_auth('old'))
    with store.engine.begin() as connection:
        connection.execute(store.table.insert(), [
            {
                'project_id': TEST_PROJECT_ID + 1,
                'type': store.credential_type,
                'account_id': 'account-a',
                'cookie': serialize_credential(make_auth('other-project')),
            },
            {
                'project_id': TEST_PROJECT_ID,
                'type': 'another_crawler',
                'account_id': 'account-a',
                'cookie': serialize_credential(make_auth('other-type')),
            },
            {
                'project_id': TEST_PROJECT_ID,
                'type': store.credential_type,
                'account_id': 'account-b',
                'cookie': serialize_credential(make_auth('other-account')),
            },
        ])

    updated = store.insert('account-a', make_auth('new'))

    with store.engine.connect() as connection:
        rows = connection.execute(store.table.select()).mappings().all()
    cookies = {
        (row['project_id'], row['type'], row['account_id']): row['cookie']
        for row in rows
    }
    assert updated.row_id == target.row_id
    assert len(rows) == 4
    assert cookies[(TEST_PROJECT_ID, store.credential_type, 'account-a')].startswith(
        'sessionid=new;'
    )
    assert cookies[(TEST_PROJECT_ID + 1, store.credential_type, 'account-a')].startswith(
        'sessionid=other-project;'
    )
    assert cookies[(TEST_PROJECT_ID, 'another_crawler', 'account-a')].startswith(
        'sessionid=other-type;'
    )
    assert cookies[(TEST_PROJECT_ID, store.credential_type, 'account-b')].startswith(
        'sessionid=other-account;'
    )


def test_store_delete_credential_only_deletes_exact_snapshot():
    store = make_store()
    stale_record = store.insert('account-a', make_auth('old'))
    updated_record = store.insert('account-a', make_auth('new'))
    store.insert('account-b', make_auth('other'))

    # 同一行已被扫码更新时，旧认证快照不得删除新凭证。
    assert updated_record.row_id == stale_record.row_id
    assert store.delete_credential(stale_record) is False
    assert {record.account_id for record in store.load_latest()} == {'account-a', 'account-b'}

    assert store.delete_credential(updated_record) is True
    assert [record.account_id for record in store.load_latest()] == ['account-b']


def test_store_isolates_accounts_by_fixed_project_id():
    """只加载项目 22，其他项目的同名账号不能覆盖当前凭证。"""
    store = make_store()
    with store.engine.begin() as connection:
        connection.execute(store.table.insert().values(
            project_id=TEST_PROJECT_ID,
            type=store.credential_type,
            account_id='account-a',
            cookie=serialize_credential(make_auth('project-22')),
        ))
        connection.execute(store.table.insert().values(
            project_id=23,
            type=store.credential_type,
            account_id='account-a',
            cookie=serialize_credential(make_auth('project-23')),
        ))

    records = store.load_latest()

    assert len(records) == 1
    assert records[0].account_id == 'account-a'
    assert records[0].auth.cookie['sessionid'] == 'project-22'


def test_invalid_latest_version_does_not_fall_back_to_old_cookie():
    store = make_store()
    store.insert('account-a', make_auth('valid'))
    with store.engine.begin() as connection:
        connection.execute(store.table.insert().values(
            project_id=TEST_PROJECT_ID,
            type=store.credential_type,
            account_id='account-a',
            cookie='{invalid-json',
        ))

    record = store.load_latest()[0]
    assert record.is_valid is False
    assert record.invalid_reason == 'invalid_json'


def test_cookie_without_api_fingerprint_is_invalid():
    store = make_store()
    payload = '{"version":1,"cookie":"sessionid=login-only"}'
    with store.engine.begin() as connection:
        connection.execute(store.table.insert().values(
            project_id=TEST_PROJECT_ID,
            type=store.credential_type,
            account_id='account-a',
            cookie=payload,
        ))

    record = store.load_latest()[0]
    assert record.is_valid is False
    assert record.invalid_reason == 'missing_fingerprint_cookie'


def test_legacy_plain_cookie_string_is_loaded_as_valid_account():
    """历史原始 Cookie 无需迁移即可进入账号池。"""
    store = make_store()
    with store.engine.begin() as connection:
        connection.execute(store.table.insert().values(
            project_id=TEST_PROJECT_ID,
            type=store.credential_type,
            account_id='legacy-account',
            cookie='sessionid=legacy; s_v_web_id=fp-legacy; ttwid=tw-legacy',
        ))

    record = store.load_latest()[0]

    assert record.is_valid is True
    assert record.account_id == 'legacy-account'
    assert record.auth.cookie['sessionid'] == 'legacy'
    assert record.auth.cookie['s_v_web_id'] == 'fp-legacy'


def test_json_quoted_plain_cookie_string_is_supported():
    """兼容被 JSON 字符串包装的历史 Cookie。"""
    store = make_store()
    with store.engine.begin() as connection:
        connection.execute(store.table.insert().values(
            project_id=TEST_PROJECT_ID,
            type=store.credential_type,
            account_id='quoted-account',
            cookie='"sessionid=quoted; s_v_web_id=fp-quoted"',
        ))

    record = store.load_latest()[0]

    assert record.is_valid is True
    assert record.auth.cookie['sessionid'] == 'quoted'


def test_pool_round_robin_and_cooldown_retry_after():
    now = [1_700_000_000.0]
    pool = AccountPool(
        [
            make_record(1, 'account-a', make_auth('a')),
            make_record(2, 'account-b', make_auth('b')),
        ],
        cooldown_seconds=300,
        clock=lambda: now[0],
    )

    with pool.acquire() as first:
        assert first.account_id == 'account-a'
    with pool.acquire() as second:
        assert second.account_id == 'account-b'

    pool.mark_auth_failure('account-a')
    pool.mark_auth_failure('account-b')
    with pytest.raises(NoAvailableAccountError) as error:
        with pool.acquire():
            pass
    assert error.value.retry_after_seconds == 300
    assert pool.stats()['cooling'] == 2


def test_account_is_removed_after_configured_cooldowns_until_credential_refresh():
    now = [1_700_000_000.0]
    old_record = make_record(10, 'account-a', make_auth('old'))
    pool = AccountPool(
        [old_record],
        cooldown_seconds=5,
        cooldown_failure_limit=3,
        clock=lambda: now[0],
    )

    for failure_count in range(1, 4):
        pool.mark_risk_control('account-a', credential_id=old_record.row_id)
        if failure_count < 3:
            assert pool.list_accounts()[0]['status'] == 'cooling'
            now[0] += 6
            assert pool.list_accounts()[0]['status'] == 'available'

    assert pool.list_accounts() == []
    assert pool.stats()['total'] == 0
    # 同一凭证不会被数据库定时刷新重新加入。
    assert pool.refresh([old_record]) == 0
    pool.upsert(old_record)
    assert pool.list_accounts()[0]['status'] == 'available'

    new_record = make_record(11, 'account-a', make_auth('new'))
    assert pool.refresh([new_record]) == 1
    assert pool.list_accounts()[0]['status'] == 'available'


def test_zero_cooldown_failure_limit_disables_account_removal():
    pool = AccountPool(
        [make_record(1, 'account-a', make_auth('a'))],
        cooldown_seconds=0,
        cooldown_failure_limit=0,
    )

    for _ in range(5):
        pool.mark_auth_failure('account-a', credential_id=1)

    assert pool.stats() == {'total': 1, 'available': 1, 'cooling': 0, 'invalid': 0}


def test_from_store_deletes_database_record_after_cooldown_limit():
    store = make_store()
    record = store.insert('account-a', make_auth('a'))
    pool = AccountPool.from_store(
        store,
        max_concurrent_per_account=1,
        cooldown_seconds=0,
        cooldown_failure_limit=1,
    )

    with pool.acquire() as lease:
        pool.mark_risk_control(lease.account_id, lease.row_id, lease.auth)

    assert pool.list_accounts() == []
    assert store.load_latest() == []


def test_old_lease_cannot_remove_same_row_after_explicit_credential_update():
    old_record = make_record(1, 'account-a', make_auth('old'))
    pool = AccountPool([old_record], cooldown_failure_limit=1)
    new_record = make_record(1, 'account-a', make_auth('new'))
    pool.upsert(new_record)

    assert pool.mark_risk_control('account-a', old_record.row_id, old_record.auth) is None
    assert pool.list_accounts()[0]['status'] == 'available'


def test_pool_can_acquire_specific_account_without_advancing_round_robin():
    pool = AccountPool([
        make_record(1, 'account-a', make_auth('a')),
        make_record(2, 'account-b', make_auth('b')),
    ])

    with pool.acquire(account_id='account-b') as pinned:
        assert pinned.account_id == 'account-b'
        assert pinned.auth.label == 'b'
    with pool.acquire() as normal:
        assert normal.account_id == 'account-a'


def test_pinned_authentication_failure_does_not_fail_over():
    attempted = []

    class ExpiredAPI:
        def search_some_general_work(self, auth, *args):
            attempted.append(auth.label)
            raise DouyinAuthenticationError('expired')

    class SearchRequest:
        query = '测试'
        limit = 1
        sort_type = '0'
        publish_time = '0'
        filter_duration = ''
        search_range = '0'
        content_type = '0'
        target_account_id = 'account-a'

    pool = AccountPool([
        make_record(1, 'account-a', make_auth('a')),
        make_record(2, 'account-b', make_auth('b')),
    ])
    service = SpiderService(
        account_pool=pool,
        douyin_api=ExpiredAPI(),
        test_account_pinning_enabled=True,
    )

    with pytest.raises(NoAvailableAccountError):
        service.search_works(SearchRequest(), 'request-id')

    assert attempted == ['a']
    statuses = {item['account_id']: item['status'] for item in pool.list_accounts()}
    assert statuses == {'account-a': 'cooling', 'account-b': 'available'}


def test_account_list_never_contains_credentials():
    pool = AccountPool([make_record(1, 'account-a', make_auth('very-secret'))])
    accounts = pool.list_accounts()
    public_data = str(accounts)
    assert 'very-secret' not in public_data
    assert 'cookie' not in public_data.lower()
    assert set(accounts[0]) == {
        'account_id',
        'credential_id',
        'updated_at',
        'status',
        'cooldown_until',
    }


def test_pool_refresh_merges_only_newer_database_versions():
    """定时刷新应保留运行状态，并禁止旧查询覆盖扫码新版本。"""
    old_record = make_record(1, 'account-a', make_auth('old'))
    pool = AccountPool([old_record], cooldown_seconds=300)
    pool.mark_auth_failure('account-a', credential_id=1)

    assert pool.refresh([old_record]) == 0
    assert pool.list_accounts()[0]['status'] == 'cooling'

    new_record = make_record(2, 'account-a', make_auth('new'))
    invalid_record = make_record(3, 'account-b', None, 'invalid_json')
    assert pool.refresh([new_record, invalid_record]) == 2

    accounts = {item['account_id']: item for item in pool.list_accounts()}
    assert accounts['account-a']['credential_id'] == 2
    assert accounts['account-a']['status'] == 'available'
    assert accounts['account-b']['status'] == 'invalid'

    # 模拟查询早于并发扫码完成，旧 ID 不得降级当前认证。
    assert pool.refresh([old_record]) == 0
    with pool.acquire() as lease:
        assert lease.account_id == 'account-a'
        assert lease.credential_id == 2
        assert lease.auth.label == 'new'


def test_periodic_refresh_recovers_after_database_failure_without_leaking(capfd):
    """单次数据库失败不应终止后续刷新，也不能记录异常正文。"""
    record = make_record(1, 'account-a', make_auth('fresh'))

    class FlakyStore:
        def __init__(self):
            self.calls = 0

        def load_latest(self):
            self.calls += 1
            if self.calls == 1:
                raise CredentialStoreError('password=must-not-leak')
            return [record]

    store = FlakyStore()
    pool = AccountPool([])

    async def run_refresh():
        task = asyncio.create_task(
            main._refresh_account_pool_periodically(store, pool, 0.01)
        )
        deadline = asyncio.get_running_loop().time() + 1
        while pool.stats()['total'] == 0:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError('定时刷新未加载账号')
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run_refresh())
    captured = capfd.readouterr()

    assert store.calls >= 2
    assert pool.list_accounts()[0]['credential_id'] == 1
    assert 'must-not-leak' not in captured.out
    assert 'must-not-leak' not in captured.err


def test_spider_retries_once_with_next_account_on_auth_failure():
    class FailoverAPI:
        def get_work_info(self, auth, url):
            if auth.label == 'bad':
                raise DouyinAuthenticationError('secret must not leak')
            return {'aweme_detail': make_work('101')}

    pool = AccountPool([
        make_record(1, 'bad-account', make_auth('bad')),
        make_record(2, 'good-account', make_auth('good')),
    ])
    service = SpiderService(account_pool=pool, douyin_api=FailoverAPI())
    result = service.get_works(['https://www.douyin.com/video/101'], 'request-id')

    assert result['account_id'] == 'good-account'
    assert result['failover_count'] == 1
    assert pool.list_accounts()[0]['status'] == 'cooling'


def test_comment_request_retries_with_next_account_on_auth_failure():
    calls = []

    class CommentFailoverAPI:
        def get_work_out_comment(self, auth, url, cursor, count):
            calls.append((auth.label, url, cursor, count))
            if auth.label == 'bad':
                raise DouyinAuthenticationError('登录已失效')
            return {'comments': [{'cid': 'comment-1'}], 'cursor': 10, 'has_more': 1}

    pool = AccountPool([
        make_record(1, 'bad-account', make_auth('bad')),
        make_record(2, 'good-account', make_auth('good')),
    ])
    service = SpiderService(account_pool=pool, douyin_api=CommentFailoverAPI())

    result = service.get_work_comments(
        'https://www.douyin.com/video/101',
        0,
        10,
        'request-id',
    )

    assert result['account_id'] == 'good-account'
    assert result['failover_count'] == 1
    assert result['items'][0]['cid'] == 'comment-1'
    assert pool.list_accounts()[0]['status'] == 'cooling'
    assert calls == [
        ('bad', 'https://www.douyin.com/video/101', '0', '10'),
        ('good', 'https://www.douyin.com/video/101', '0', '10'),
    ]


def test_comment_non_authentication_failure_does_not_switch_account():
    calls = []

    class CommentFailureAPI:
        def get_work_out_comment(self, auth, url, cursor, count):
            calls.append(auth.label)
            raise RuntimeError('temporary upstream failure')

    pool = AccountPool([
        make_record(1, 'account-a', make_auth('a')),
        make_record(2, 'account-b', make_auth('b')),
    ])
    service = SpiderService(account_pool=pool, douyin_api=CommentFailureAPI())

    with pytest.raises(UpstreamServiceError):
        service.get_work_comments(
            'https://www.douyin.com/video/101',
            0,
            10,
            'request-id',
        )

    assert calls == ['a']
    assert pool.stats()['available'] == 2
    assert pool.stats()['cooling'] == 0


def test_non_authentication_failure_does_not_cool_or_change_account():
    class ContentFailureAPI:
        def get_work_info(self, auth, url):
            raise ValueError('作品不存在')

    pool = AccountPool([
        make_record(1, 'account-a', make_auth('a')),
        make_record(2, 'account-b', make_auth('b')),
    ])
    service = SpiderService(account_pool=pool, douyin_api=ContentFailureAPI())

    with pytest.raises(UpstreamServiceError):
        service.get_works(['https://www.douyin.com/video/404'], 'request-id')
    assert pool.stats()['available'] == 2
    assert pool.stats()['cooling'] == 0


def test_old_inflight_failure_does_not_cool_refreshed_credential():
    started = threading.Event()
    resume = threading.Event()

    class RefreshRaceAPI:
        def get_work_info(self, auth, url):
            if auth.label == 'old':
                started.set()
                assert resume.wait(timeout=2)
                raise DouyinAuthenticationError('旧凭证已失效')
            return {'aweme_detail': make_work('new-version')}

    old_auth = make_auth('old')
    new_auth = make_auth('new')
    pool = AccountPool([make_record(1, 'account-a', old_auth)])
    service = SpiderService(account_pool=pool, douyin_api=RefreshRaceAPI())

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.get_works,
            ['https://www.douyin.com/video/101'],
            'request-id',
        )
        assert started.wait(timeout=2)
        pool.upsert('account-a', new_auth, make_record(2, 'account-a', new_auth))
        resume.set()
        result = future.result(timeout=2)

    assert result['account_id'] == 'account-a'
    assert result['failover_count'] == 1
    assert pool.stats()['available'] == 1
    assert pool.stats()['cooling'] == 0
    assert pool.list_accounts()[0]['credential_id'] == 2


def test_single_authentication_failure_returns_pool_retry_after():
    class ExpiredAPI:
        def get_work_info(self, auth, url):
            raise DouyinAuthenticationError('登录已失效')

    now = [1_700_000_000.0]
    pool = AccountPool(
        [make_record(1, 'account-a', make_auth('a'))],
        cooldown_seconds=45,
        clock=lambda: now[0],
    )
    service = SpiderService(account_pool=pool, douyin_api=ExpiredAPI())

    with pytest.raises(NoAvailableAccountError) as captured:
        service.get_works(['https://www.douyin.com/video/101'], 'request-id')

    assert captured.value.retry_after_seconds == 45
    assert pool.stats()['cooling'] == 1


def test_two_authentication_failures_leave_pool_unavailable_with_retry_after():
    attempted = []

    class ExpiredAPI:
        def get_work_info(self, auth, url):
            attempted.append(auth.label)
            raise DouyinAuthenticationError('登录已失效')

    pool = AccountPool([
        make_record(1, 'account-a', make_auth('a')),
        make_record(2, 'account-b', make_auth('b')),
    ], cooldown_seconds=30)
    service = SpiderService(account_pool=pool, douyin_api=ExpiredAPI())

    with pytest.raises(NoAvailableAccountError) as captured:
        service.get_works(['https://www.douyin.com/video/101'], 'request-id')

    assert attempted == ['a', 'b']
    assert captured.value.retry_after_seconds in (29, 30)
    assert pool.stats()['cooling'] == 2


def test_request_queue_timeout_is_bounded_and_slot_is_released_after_completion():
    started = threading.Event()
    release = threading.Event()

    class BlockingAPI:
        def get_work_info(self, auth, url):
            started.set()
            assert release.wait(timeout=2)
            return {'aweme_detail': make_work('bounded')}

    pool = AccountPool([make_record(1, 'account-a', make_auth('a'))])
    service = SpiderService(
        account_pool=pool,
        douyin_api=BlockingAPI(),
        max_concurrent=1,
        account_acquire_timeout_seconds=0.05,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            service.get_works,
            ['https://www.douyin.com/video/101'],
            'first',
        )
        assert started.wait(timeout=2)
        started_at = time.monotonic()
        with pytest.raises(NoAvailableAccountError):
            service.get_works(['https://www.douyin.com/video/102'], 'second')
        assert time.monotonic() - started_at < 0.5
        release.set()
        assert first.result(timeout=2)['success_count'] == 1

    assert service.get_works(
        ['https://www.douyin.com/video/103'], 'third'
    )['success_count'] == 1


def test_different_accounts_can_run_in_parallel():
    both_started = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0

    class ParallelAPI:
        def get_work_info(self, auth, url):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    both_started.set()
            assert both_started.wait(timeout=2)
            with lock:
                active -= 1
            return {'aweme_detail': make_work(auth.label)}

    pool = AccountPool([
        make_record(1, 'account-a', make_auth('a')),
        make_record(2, 'account-b', make_auth('b')),
    ])
    service = SpiderService(account_pool=pool, douyin_api=ParallelAPI(), max_concurrent=2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                service.get_works,
                [f'https://www.douyin.com/video/{index}'],
                str(index),
            )
            for index in range(2)
        ]
        results = [future.result(timeout=2) for future in futures]

    assert maximum == 2
    assert {result['account_id'] for result in results} == {'account-a', 'account-b'}

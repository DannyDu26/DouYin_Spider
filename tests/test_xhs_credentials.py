"""验证 Cookie/remark 原子保存、重启恢复和并发版本保护。"""
import base64
import json
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import StaticPool

from app.account_pool import AccountPool
from app.account_store import CredentialFormatError, MySQLCredentialStore
from app.xhs_credentials import XHS_CREDENTIAL_TYPE, XhsCredentialCodec
from test_xhs_api import make_auth


def make_store():
    engine = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    store = MySQLCredentialStore(engine, 22, XHS_CREDENTIAL_TYPE, codec=XhsCredentialCodec())
    store.table.create(engine)
    return store


def test_state_round_trip_and_plain_cookie_column():
    store = make_store()
    auth = make_auth()
    secret = base64.b64encode(bytes(range(48))).decode()
    auth.profile.update_storage(local_storage={'webSsk': json.dumps({'xhs-pc-web': secret})})
    auth.profile.session.mns_seq = 37
    auth.profile.session.last_tiga_update_time = 1720000000100
    auth._cookie_store.update('so.xiaohongshu.com', {'acw_tc': 'search-only'})
    record = store.insert('one', auth)
    with store.engine.connect() as connection:
        row = connection.execute(select(store.table.c.cookie, store.table.c.remark)).first()
    assert row.cookie.startswith('a1=') and not row.cookie.startswith('{')
    assert json.loads(row.remark)['local_storage']['webSsk']
    restored = store.load_latest()[0].auth
    assert restored.profile.local_storage['webSsk'] == auth.profile.local_storage['webSsk']
    assert restored.profile.session.mns_seq == 37
    assert restored._cookie_store.export_state() == auth._cookie_store.export_state()
    assert restored.user_id == auth.user_id
    assert restored.login_verified is False
    restored.close()
    auth.close()
    store.close()


def test_relogin_same_row_refreshes_pool_and_rejects_old_snapshot():
    store = make_store()
    old = store.insert('one', make_auth('old'))
    pool = AccountPool([old], manage_auth_resources=True)
    new = store.insert('one', make_auth('new'))
    assert new.row_id == old.row_id and new.revision > old.revision
    assert pool.refresh([new]) == 1
    assert store.save_snapshot(old, old.auth) is None
    stale = store.codec.decode(old.stored_cookie, old.stored_remark)
    assert pool.refresh([replace(old, auth=stale)]) == 0
    assert pool.list_accounts()[0]['account_id'] == 'one'
    pool.close()
    store.close()


def test_snapshot_cas_prevents_duplicate_writer():
    store = make_store()
    record = store.insert('one', make_auth())
    updated = store.save_snapshot(record, record.auth)
    assert updated and updated.revision == record.revision + 1
    assert store.save_snapshot(record, record.auth) is None
    record.auth.close()
    store.close()


def test_old_client_closes_after_active_lease():
    store = make_store()
    old = store.insert('one', make_auth('old'))
    closed = []
    old.auth.close = lambda: closed.append('old')
    pool = AccountPool([old], manage_auth_resources=True)
    with pool.acquire() as lease:
        new = store.insert('one', make_auth('new'))
        pool.upsert(new)
        assert not closed and lease.auth is old.auth
    assert closed == ['old']
    pool.close()
    store.close()


def test_platform_records_do_not_mix():
    store = make_store()
    auth = make_auth()
    store.insert('one', auth)
    douyin = MySQLCredentialStore(store.engine, 22)
    assert douyin.load_latest() == []
    auth.close()
    store.close()


@pytest.mark.parametrize('remark', ['bad-json', '{}', '{"version":2}', '{"version":1,"revision":-1}'])
def test_invalid_metadata_rejected(remark):
    with pytest.raises(CredentialFormatError):
        XhsCredentialCodec().decode('a1=a; web_session=s', remark)


def test_startup_accepts_text_and_keeps_shared_engine():
    from app.xhs_service import create_xhs_service
    store = make_store()
    service = create_xhs_service(store)
    assert service.account_pool.stats()['total'] == 0
    service.close()
    assert store.check_connection()
    store.close()


def test_startup_requires_remark_migration():
    from app.xhs_service import create_xhs_service
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE crawler_cookie (id INTEGER PRIMARY KEY, remark VARCHAR(100))'))
    store = MySQLCredentialStore(engine, 22)
    with pytest.raises(RuntimeError, match='001_xhs_remark_text.sql'):
        create_xhs_service(store)
    store.close()

# coding=utf-8
"""MySQL TLS 与超时配置测试，不连接真实数据库。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError

from app import account_store
from app.account_store import (
    CredentialStoreConfigurationError,
    MySQLCredentialStore,
)


MYSQL_ENV_NAMES = (
    'MYSQL_HOST',
    'MYSQL_PORT',
    'MYSQL_DATABASE',
    'CRAWLER_PROJECT_ID',
    'MYSQL_USER',
    'MYSQL_PASSWORD',
    'MYSQL_POOL_RECYCLE_SECONDS',
    'MYSQL_CONNECT_TIMEOUT_SECONDS',
    'MYSQL_READ_TIMEOUT_SECONDS',
    'MYSQL_WRITE_TIMEOUT_SECONDS',
    'MYSQL_TIME_ZONE',
    'MYSQL_SSL_DISABLED',
    'MYSQL_SSL_CA',
    'MYSQL_SSL_CERT',
    'MYSQL_SSL_KEY',
)


@pytest.fixture
def engine_spy(monkeypatch):
    """捕获 create_engine 参数，隔离本机环境文件和 PyMySQL。"""
    for name in MYSQL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(account_store, 'load_environment', lambda: None)
    monkeypatch.setattr(account_store, 'get_app_env', lambda: 'dev')
    monkeypatch.setenv('MYSQL_USER', 'crawler-user')
    monkeypatch.setenv('MYSQL_PASSWORD', 'secret-password')
    monkeypatch.setenv('MYSQL_DATABASE', 'auto_crawler')
    monkeypatch.setenv('CRAWLER_PROJECT_ID', '22')

    captured = {}
    fake_engine = object()

    def fake_create_engine(url, **kwargs):
        captured['url'] = url
        captured['kwargs'] = kwargs
        return fake_engine

    monkeypatch.setattr(account_store, 'create_engine', fake_create_engine)
    return captured, fake_engine


@pytest.mark.parametrize('host', ['localhost', 'localhost.', 'api.localhost', '127.0.0.1', '::1'])
def test_loopback_mysql_allows_no_tls(monkeypatch, engine_spy, host):
    captured, fake_engine = engine_spy
    monkeypatch.setenv('MYSQL_HOST', host)

    store = MySQLCredentialStore.from_env()

    assert store.engine is fake_engine
    assert captured['kwargs']['connect_args'] == {
        'connect_timeout': 10,
        'read_timeout': 30,
        'write_timeout': 30,
    }
    assert captured['url'].database == 'auto_crawler'
    assert store.project_id == 22


def test_database_and_project_id_are_loaded_from_environment(monkeypatch, engine_spy):
    captured, _ = engine_spy
    monkeypatch.setenv('MYSQL_HOST', 'localhost')
    monkeypatch.setenv('MYSQL_DATABASE', 'custom_crawler')
    monkeypatch.setenv('CRAWLER_PROJECT_ID', '37')

    store = MySQLCredentialStore.from_env()

    assert captured['url'].database == 'custom_crawler'
    assert store.project_id == 37


@pytest.mark.parametrize('value', ['', '0', '-1', 'not-a-number'])
def test_project_id_must_be_a_positive_integer(monkeypatch, engine_spy, value):
    monkeypatch.setenv('MYSQL_HOST', 'localhost')
    monkeypatch.setenv('CRAWLER_PROJECT_ID', value)

    with pytest.raises(CredentialStoreConfigurationError, match='CRAWLER_PROJECT_ID'):
        MySQLCredentialStore.from_env()


def test_remote_mysql_requires_tls_by_default(monkeypatch, engine_spy):
    captured, _ = engine_spy
    monkeypatch.setenv('MYSQL_HOST', 'mysql.internal.example')

    with pytest.raises(CredentialStoreConfigurationError, match='MYSQL_SSL_CA'):
        MySQLCredentialStore.from_env()

    assert captured == {}


def test_explicit_development_exception_allows_plain_remote_mysql(monkeypatch, engine_spy):
    captured, _ = engine_spy
    monkeypatch.setenv('MYSQL_HOST', 'mysql.dev.internal')
    monkeypatch.setenv('MYSQL_SSL_DISABLED', 'true')

    MySQLCredentialStore.from_env()

    assert 'ssl' not in captured['kwargs']['connect_args']


def test_production_allows_explicit_plain_mysql(monkeypatch, engine_spy):
    captured, _ = engine_spy
    monkeypatch.setattr(account_store, 'get_app_env', lambda: 'prod')
    monkeypatch.setenv('MYSQL_HOST', 'mysql.internal.example')
    monkeypatch.setenv('MYSQL_SSL_DISABLED', 'true')

    MySQLCredentialStore.from_env()

    assert 'ssl' not in captured['kwargs']['connect_args']


def test_production_rejects_empty_database_password(monkeypatch, engine_spy):
    monkeypatch.setattr(account_store, 'get_app_env', lambda: 'prod')
    monkeypatch.setenv('MYSQL_PASSWORD', '   ')

    with pytest.raises(CredentialStoreConfigurationError, match='MYSQL_PASSWORD 不能为空'):
        MySQLCredentialStore.from_env()


def test_ca_enables_verified_tls(monkeypatch, engine_spy):
    captured, _ = engine_spy
    monkeypatch.setenv('MYSQL_HOST', 'mysql.internal.example')
    monkeypatch.setenv('MYSQL_SSL_CA', 'C:/certs/ca.pem')

    MySQLCredentialStore.from_env()

    assert captured['kwargs']['connect_args']['ssl'] == {
        'ca': 'C:/certs/ca.pem',
        'verify_mode': 'required',
        'check_hostname': True,
    }


@pytest.mark.parametrize('env_name', ['MYSQL_SSL_CERT', 'MYSQL_SSL_KEY'])
def test_client_certificate_must_be_paired(monkeypatch, engine_spy, env_name):
    monkeypatch.setenv('MYSQL_HOST', 'mysql.internal.example')
    monkeypatch.setenv(env_name, 'C:/certs/client.pem')

    with pytest.raises(CredentialStoreConfigurationError, match='必须同时配置'):
        MySQLCredentialStore.from_env()


def test_client_certificate_pair_still_requires_ca(monkeypatch, engine_spy):
    monkeypatch.setenv('MYSQL_HOST', 'mysql.internal.example')
    monkeypatch.setenv('MYSQL_SSL_CERT', 'C:/certs/client.pem')
    monkeypatch.setenv('MYSQL_SSL_KEY', 'C:/certs/client.key')

    with pytest.raises(CredentialStoreConfigurationError, match='MYSQL_SSL_CA'):
        MySQLCredentialStore.from_env()


def test_tls_options_conflict_with_explicit_ssl_disable(monkeypatch, engine_spy):
    monkeypatch.setenv('MYSQL_HOST', 'mysql.internal.example')
    monkeypatch.setenv('MYSQL_SSL_CA', 'C:/certs/ca.pem')
    monkeypatch.setenv('MYSQL_SSL_DISABLED', 'true')

    with pytest.raises(CredentialStoreConfigurationError, match='不能与'):
        MySQLCredentialStore.from_env()


def test_all_tls_options_and_custom_timeouts_are_forwarded(monkeypatch, engine_spy):
    captured, _ = engine_spy
    monkeypatch.setenv('MYSQL_HOST', '10.0.0.8')
    monkeypatch.setenv('MYSQL_SSL_CA', 'C:/certs/ca.pem')
    monkeypatch.setenv('MYSQL_SSL_CERT', 'C:/certs/client.pem')
    monkeypatch.setenv('MYSQL_SSL_KEY', 'C:/certs/client.key')
    monkeypatch.setenv('MYSQL_CONNECT_TIMEOUT_SECONDS', '7')
    monkeypatch.setenv('MYSQL_READ_TIMEOUT_SECONDS', '17')
    monkeypatch.setenv('MYSQL_WRITE_TIMEOUT_SECONDS', '19')

    MySQLCredentialStore.from_env()

    assert captured['kwargs']['connect_args'] == {
        'connect_timeout': 7,
        'read_timeout': 17,
        'write_timeout': 19,
        'ssl': {
            'ca': 'C:/certs/ca.pem',
            'cert': 'C:/certs/client.pem',
            'key': 'C:/certs/client.key',
            'verify_mode': 'required',
            'check_hostname': True,
        },
    }
    assert captured['kwargs']['pool_pre_ping'] is True
    assert captured['kwargs']['pool_recycle'] == 1800


def test_mysql_session_time_zone_is_forwarded(monkeypatch, engine_spy):
    captured, _ = engine_spy
    monkeypatch.setenv('MYSQL_HOST', 'localhost')
    monkeypatch.setenv('MYSQL_TIME_ZONE', '+08:00')

    MySQLCredentialStore.from_env()

    assert captured['kwargs']['connect_args']['init_command'] == (
        "SET time_zone = '+08:00'"
    )


@pytest.mark.parametrize('value', ['Asia/Shanghai', '8:00', '+15:00'])
def test_mysql_session_time_zone_must_be_valid_offset(
        monkeypatch, engine_spy, value,
):
    monkeypatch.setenv('MYSQL_HOST', 'localhost')
    monkeypatch.setenv('MYSQL_TIME_ZONE', value)

    with pytest.raises(CredentialStoreConfigurationError, match='MYSQL_TIME_ZONE'):
        MySQLCredentialStore.from_env()


@pytest.mark.parametrize(
    'name',
    [
        'MYSQL_CONNECT_TIMEOUT_SECONDS',
        'MYSQL_READ_TIMEOUT_SECONDS',
        'MYSQL_WRITE_TIMEOUT_SECONDS',
    ],
)
@pytest.mark.parametrize('value', ['0', '-1', 'not-a-number'])
def test_timeouts_must_be_positive_integers(monkeypatch, engine_spy, name, value):
    monkeypatch.setenv('MYSQL_HOST', 'localhost')
    monkeypatch.setenv(name, value)

    with pytest.raises(CredentialStoreConfigurationError, match=name):
        MySQLCredentialStore.from_env()


def test_ssl_disabled_must_be_explicit_boolean(monkeypatch, engine_spy):
    monkeypatch.setenv('MYSQL_HOST', 'localhost')
    monkeypatch.setenv('MYSQL_SSL_DISABLED', 'sometimes')

    with pytest.raises(CredentialStoreConfigurationError, match='MYSQL_SSL_DISABLED'):
        MySQLCredentialStore.from_env()


def test_engine_initialization_error_does_not_expose_secrets(monkeypatch, engine_spy):
    monkeypatch.setenv('MYSQL_HOST', 'mysql.internal.example')
    monkeypatch.setenv('MYSQL_SSL_CA', 'C:/secret/company-ca.pem')

    def fail_to_create_engine(*args, **kwargs):
        raise SQLAlchemyError('driver failed')

    monkeypatch.setattr(account_store, 'create_engine', fail_to_create_engine)
    with pytest.raises(CredentialStoreConfigurationError) as captured:
        MySQLCredentialStore.from_env()

    message = str(captured.value)
    assert 'secret-password' not in message
    assert 'company-ca.pem' not in message


def test_mysql_latest_query_uses_binary_values_for_isolation():
    """过滤、分组和关联均需区分大小写及尾随空格。"""
    project_id = 22
    store = MySQLCredentialStore(create_engine('sqlite://'), project_id=project_id)

    statement = store._latest_statement('mysql')
    sql = str(statement.compile(
        dialect=mysql.dialect(),
        compile_kwargs={'literal_binds': True},
    )).lower()
    account_expression = 'cast(crawler_cookie.account_id as binary)'
    assert sql.count(account_expression) >= 4
    assert f'group by {account_expression}' in sql
    assert (
        f'{account_expression} = cast(anon_1.account_id as binary)'
        in sql
    )
    assert sql.count(
        "cast(crawler_cookie.type as binary) = 'douyin_api_account_v1'"
    ) == 2
    assert sql.count(f'crawler_cookie.project_id = {project_id}') == 2

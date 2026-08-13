# coding=utf-8
"""抖音多账号凭证的 MySQL 持久化仓库。"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, String, Table, Text, and_, cast, func, select, text
from sqlalchemy.dialects.mysql import BINARY as MYSQL_BINARY
from sqlalchemy.engine import Engine, URL, create_engine
from sqlalchemy.exc import SQLAlchemyError

from builder.auth import DouyinAuth
from env_config import get_app_env, load_environment


CREDENTIAL_TYPE = 'douyin_api_account_v1'
CREDENTIAL_VERSION = 1
ACCOUNT_ID_PATTERN = re.compile(r'^[a-z0-9_-]{1,64}$')
LOGIN_COOKIE_NAMES = frozenset({'sessionid', 'sessionid_ss', 'sid_guard', 'uid_tt', 'uid_tt_ss'})
_TRUE_VALUES = frozenset({'1', 'true', 'yes', 'on'})
_FALSE_VALUES = frozenset({'0', 'false', 'no', 'off'})
MYSQL_TIME_ZONE_PATTERN = re.compile(r'^[+-](?:(?:0\d|1[0-3]):[0-5]\d|14:00)$')


class CredentialStoreError(RuntimeError):
    """凭证仓库操作失败，异常消息不会包含数据库连接详情。"""


class CredentialStoreConfigurationError(CredentialStoreError):
    """凭证仓库配置无效。"""


class CredentialFormatError(ValueError):
    """凭证内容不符合当前版本格式。"""

    def __init__(self, message: str, code: str = 'invalid_payload'):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    """数据库中某个账号的最新凭证记录。"""

    row_id: int
    account_id: str
    created_at: datetime | None
    auth: DouyinAuth | None
    invalid_reason: str | None = None

    @property
    def id(self) -> int:
        """兼容数据库主键命名。"""
        return self.row_id

    @property
    def credential_id(self) -> int:
        """提供语义更明确的凭证版本 ID。"""
        return self.row_id

    @property
    def is_valid(self) -> bool:
        return self.auth is not None and self.invalid_reason is None


def validate_account_id(account_id: str) -> str:
    """校验可安全写入 account_id 的稳定账号别名。"""
    if not isinstance(account_id, str) or not ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise ValueError('account_id 仅允许 1-64 位小写字母、数字、下划线或连字符')
    return account_id


def _positive_env_int(name: str, default: int) -> int:
    """读取严格正整数连接参数。"""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise CredentialStoreConfigurationError(f'{name} 必须是正整数') from error
    if value <= 0:
        raise CredentialStoreConfigurationError(f'{name} 必须是正整数')
    return value


def _boolean_env(name: str, default: bool = False) -> bool:
    """解析显式布尔配置，拒绝含糊值。"""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise CredentialStoreConfigurationError(
        f'{name} 必须是 true/false、1/0、yes/no 或 on/off'
    )


def _is_loopback_host(host: str) -> bool:
    """仅识别明确的本机地址，不执行可能阻塞的 DNS 查询。"""
    normalized = host.strip().lower().rstrip('.')
    if normalized == 'localhost' or normalized.endswith('.localhost'):
        return True
    normalized = normalized.removeprefix('[').removesuffix(']')
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _parse_cookie_string(cookie_str: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_str.split(';'):
        name, separator, value = part.strip().partition('=')
        if separator and name:
            cookies[name] = value.strip()
    if not cookies:
        raise CredentialFormatError('凭证缺少有效 Cookie', 'missing_cookie')
    return cookies


def _normalize_cookie_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise CredentialFormatError('cookie 必须是对象', 'invalid_cookie')
    cookies: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip() or not isinstance(raw_value, str):
            raise CredentialFormatError('cookie 键值必须是字符串', 'invalid_cookie')
        cookies[raw_name.strip()] = raw_value
    if not cookies:
        raise CredentialFormatError('凭证缺少有效 Cookie', 'missing_cookie')
    return cookies


def _cookie_string(cookies: Mapping[str, str]) -> str:
    return '; '.join(f'{name}={value}' for name, value in cookies.items())


def _validate_login_cookies(cookies: Mapping[str, str]) -> None:
    """匿名 Cookie 不进入可用账号池。"""
    if not any(cookies.get(name) for name in LOGIN_COOKIE_NAMES):
        raise CredentialFormatError('凭证缺少登录 Cookie', 'missing_login_cookie')
    if not cookies.get('s_v_web_id'):
        raise CredentialFormatError('凭证缺少 API 指纹 Cookie', 'missing_fingerprint_cookie')


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise CredentialFormatError(f'{key} 必须是字符串', f'invalid_{key}')
    return value


def credential_payload_from_auth(auth: Any) -> dict[str, Any]:
    """将认证对象转换为稳定、可版本化的 JSON 数据。"""
    raw_cookies = getattr(auth, 'cookie', None)
    if isinstance(raw_cookies, Mapping) and raw_cookies:
        cookies = _normalize_cookie_map(raw_cookies)
    else:
        raw_cookie_str = getattr(auth, 'cookie_str', None)
        if not isinstance(raw_cookie_str, str):
            raise CredentialFormatError('认证对象缺少 Cookie', 'missing_cookie')
        cookies = _parse_cookie_string(raw_cookie_str)

    _validate_login_cookies(cookies)
    payload = {
        'version': CREDENTIAL_VERSION,
        'cookie': cookies,
        'cookie_str': _cookie_string(cookies),
        'ticket': getattr(auth, 'ticket', None) or None,
        'ts_sign': getattr(auth, 'ts_sign', None) or None,
        'client_cert': getattr(auth, 'client_cert', None) or None,
        'private_key': getattr(auth, 'private_key', None) or None,
    }
    return normalize_credential_payload(payload)


def normalize_credential_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """兼容扫码模块输入，并统一为当前凭证版本。"""
    if not isinstance(payload, Mapping):
        raise CredentialFormatError('凭证必须是 JSON 对象')

    version = payload.get('version', payload.get('schema_version'))
    if type(version) is not int or version != CREDENTIAL_VERSION:
        raise CredentialFormatError('不支持的凭证版本', 'unsupported_version')

    raw_cookie = payload.get('cookie')
    if isinstance(raw_cookie, Mapping):
        cookies = _normalize_cookie_map(raw_cookie)
    elif isinstance(raw_cookie, str) and raw_cookie.strip():
        cookies = _parse_cookie_string(raw_cookie)
    else:
        raw_cookie_str = payload.get('cookie_str')
        if not isinstance(raw_cookie_str, str):
            raise CredentialFormatError('凭证缺少有效 Cookie', 'missing_cookie')
        cookies = _parse_cookie_string(raw_cookie_str)

    _validate_login_cookies(cookies)
    return {
        'version': CREDENTIAL_VERSION,
        'cookie': cookies,
        'cookie_str': _cookie_string(cookies),
        'ticket': _optional_string(payload, 'ticket'),
        'ts_sign': _optional_string(payload, 'ts_sign'),
        'client_cert': _optional_string(payload, 'client_cert'),
        'private_key': _optional_string(payload, 'private_key'),
    }


def serialize_credential(auth_or_payload: Any) -> str:
    """将认证对象或凭证字典序列化为标准 Cookie 字符串。"""
    if isinstance(auth_or_payload, Mapping):
        payload = normalize_credential_payload(auth_or_payload)
    else:
        payload = credential_payload_from_auth(auth_or_payload)
    # 新记录仅保存通用 Cookie 格式，历史版本化 JSON 仍由读取端兼容。
    return payload['cookie_str']


def deserialize_credential(raw_value: str) -> DouyinAuth:
    """从数据库恢复认证，并兼容历史原始 Cookie 字符串。"""
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise CredentialFormatError('凭证内容为空', 'empty_payload')
    try:
        decoded = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError) as error:
        # 历史表直接保存 Cookie；明显不像 Cookie 的损坏 JSON 仍按错误处理。
        if '=' not in raw_value:
            raise CredentialFormatError('凭证不是有效 JSON 或 Cookie', 'invalid_json') from error
        decoded = {
            'version': CREDENTIAL_VERSION,
            'cookie': raw_value,
        }
    else:
        # 兼容数据库中被 JSON 引号包裹的 Cookie 字符串。
        if isinstance(decoded, str):
            decoded = {
                'version': CREDENTIAL_VERSION,
                'cookie': decoded,
            }

    payload = normalize_credential_payload(decoded)
    auth = DouyinAuth()
    auth.perepare_auth(payload['cookie_str'], '', '')
    auth.ticket = payload['ticket']
    auth.ts_sign = payload['ts_sign']
    auth.client_cert = payload['client_cert']
    auth.private_key = payload['private_key']
    if auth.private_key:
        auth.ree_public_key = base64.b64encode(auth.private_key.encode('utf-8')).decode('ascii')
    return auth


class MySQLCredentialStore:
    """按配置的数据库和项目 ID 隔离 Cookie 凭证。"""

    def __init__(
            self,
            engine: Engine,
            project_id: int,
            credential_type: str = CREDENTIAL_TYPE,
    ):
        if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id <= 0:
            raise ValueError('project_id 必须是正整数')
        self.engine = engine
        self.credential_type = credential_type
        self.project_id = project_id
        metadata = MetaData()
        # 仅声明现有表映射，不执行建表或迁移。
        self.table = Table(
            'crawler_cookie',
            metadata,
            Column(
                'id',
                BigInteger().with_variant(Integer, 'sqlite'),
                primary_key=True,
                autoincrement=True,
            ),
            Column('project_id', Integer, nullable=False),
            Column('type', String(100), nullable=False),
            Column('account_id', String(100), nullable=False),
            Column('cookie', Text, nullable=False),
            Column('remark', String(100)),
            Column('create_time', DateTime, nullable=False, server_default=func.current_timestamp()),
        )

    @classmethod
    def from_env(cls) -> 'MySQLCredentialStore':
        """从环境变量创建启用预检测和回收的 MySQL 连接池。"""
        load_environment()
        app_env = get_app_env()
        user = (os.getenv('MYSQL_USER') or '').strip()
        password = os.getenv('MYSQL_PASSWORD')
        if not user or password is None:
            raise CredentialStoreConfigurationError('缺少 MYSQL_USER 或 MYSQL_PASSWORD')
        if app_env == 'prod' and not password.strip():
            raise CredentialStoreConfigurationError('生产环境 MYSQL_PASSWORD 不能为空')

        host = (os.getenv('MYSQL_HOST') or '120.92.180.132').strip()
        database = (os.getenv('MYSQL_DATABASE') or '').strip()
        project_id = _positive_env_int('CRAWLER_PROJECT_ID', 0)
        try:
            port = int(os.getenv('MYSQL_PORT', '33306'))
        except ValueError as error:
            raise CredentialStoreConfigurationError('MYSQL_PORT 必须是有效端口') from error
        if not host or not database or not (1 <= port <= 65535):
            raise CredentialStoreConfigurationError('MySQL 连接配置无效')

        recycle_seconds = _positive_env_int('MYSQL_POOL_RECYCLE_SECONDS', 1800)
        connect_timeout = _positive_env_int('MYSQL_CONNECT_TIMEOUT_SECONDS', 10)
        read_timeout = _positive_env_int('MYSQL_READ_TIMEOUT_SECONDS', 30)
        write_timeout = _positive_env_int('MYSQL_WRITE_TIMEOUT_SECONDS', 30)
        ssl_disabled = _boolean_env('MYSQL_SSL_DISABLED')
        ssl_options = {
            option_name: value.strip()
            for option_name, env_name in (
                ('ca', 'MYSQL_SSL_CA'),
                ('cert', 'MYSQL_SSL_CERT'),
                ('key', 'MYSQL_SSL_KEY'),
            )
            if (value := os.getenv(env_name)) and value.strip()
        }
        if ssl_disabled and ssl_options:
            raise CredentialStoreConfigurationError(
                'MYSQL_SSL_DISABLED 不能与 MySQL TLS 证书配置同时使用'
            )
        if ('cert' in ssl_options) != ('key' in ssl_options):
            raise CredentialStoreConfigurationError(
                'MYSQL_SSL_CERT 与 MYSQL_SSL_KEY 必须同时配置'
            )
        if ssl_options and 'ca' not in ssl_options:
            raise CredentialStoreConfigurationError('MySQL TLS 必须配置 MYSQL_SSL_CA')
        if not ssl_disabled and not _is_loopback_host(host) and 'ca' not in ssl_options:
            raise CredentialStoreConfigurationError(
                '远程 MySQL 必须配置 MYSQL_SSL_CA，或显式设置 MYSQL_SSL_DISABLED=true'
            )

        connect_args: dict[str, Any] = {
            'connect_timeout': connect_timeout,
            'read_timeout': read_timeout,
            'write_timeout': write_timeout,
        }
        mysql_time_zone = (os.getenv('MYSQL_TIME_ZONE') or '').strip()
        if mysql_time_zone:
            # 使用数值偏移，避免依赖 MySQL 服务器预装命名时区表。
            if not MYSQL_TIME_ZONE_PATTERN.fullmatch(mysql_time_zone):
                raise CredentialStoreConfigurationError(
                    'MYSQL_TIME_ZONE 必须是有效的 UTC 偏移，例如 +08:00'
                )
            connect_args['init_command'] = f"SET time_zone = '{mysql_time_zone}'"
        if ssl_options:
            # 强制校验证书链和主机名，避免仅加密但不验证服务端身份。
            ssl_options['verify_mode'] = 'required'
            ssl_options['check_hostname'] = True
            connect_args['ssl'] = ssl_options

        url = URL.create(
            drivername='mysql+pymysql',
            username=user,
            password=password,
            host=host,
            port=port,
            database=database,
            query={'charset': 'utf8mb4'},
        )
        try:
            engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_recycle=recycle_seconds,
                connect_args=connect_args,
            )
        except (ImportError, SQLAlchemyError) as error:
            raise CredentialStoreConfigurationError('无法初始化 MySQL 凭证仓库') from error
        return cls(engine, project_id=project_id)

    def check_connection(self) -> bool:
        """验证数据库连通性；失败时只抛出脱敏异常。"""
        try:
            with self.engine.connect() as connection:
                connection.execute(text('SELECT 1')).scalar_one()
        except SQLAlchemyError as error:
            raise CredentialStoreError('MySQL 凭证仓库连接失败') from error
        return True

    @staticmethod
    def _invalid_reason(error: Exception) -> str:
        if isinstance(error, CredentialFormatError):
            return error.code
        return 'invalid_payload'

    @staticmethod
    def _case_sensitive(column, dialect_name: str):
        """MySQL 使用二进制值比较，同时区分大小写和尾随空格。"""
        if dialect_name in {'mysql', 'mariadb'}:
            return cast(column, MYSQL_BINARY)
        return column

    def _latest_statement(self, dialect_name: str | None = None):
        """构建最新版本查询，供执行和方言级测试复用。"""
        dialect_name = dialect_name or self.engine.dialect.name
        type_column = self._case_sensitive(self.table.c.type, dialect_name)
        account_id_column = self._case_sensitive(self.table.c.account_id, dialect_name)
        credential_type = (
            self.credential_type.encode('utf-8')
            if dialect_name in {'mysql', 'mariadb'}
            else self.credential_type
        )
        latest = (
            select(
                account_id_column.label('account_id'),
                func.max(self.table.c.id).label('latest_id'),
            )
            .where(
                self.table.c.project_id == self.project_id,
                type_column == credential_type,
                account_id_column.is_not(None),
            )
            .group_by(account_id_column)
            .subquery()
        )
        latest_account_id = self._case_sensitive(latest.c.account_id, dialect_name)
        return (
            select(
                self.table.c.id,
                self.table.c.cookie,
                self.table.c.account_id,
                self.table.c.create_time,
            )
            .join(
                latest,
                and_(
                    self.table.c.id == latest.c.latest_id,
                    account_id_column == latest_account_id,
                ),
            )
            .where(
                self.table.c.project_id == self.project_id,
                type_column == credential_type,
            )
            .order_by(self.table.c.id)
        )

    def load_latest(self) -> list[CredentialRecord]:
        """加载项目 22 中每个 account_id 的最大 ID。"""
        statement = self._latest_statement()
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise CredentialStoreError('加载 MySQL 账号凭证失败') from error

        records: list[CredentialRecord] = []
        for row in rows:
            account_id = row['account_id'] or ''
            try:
                validate_account_id(account_id)
                auth = deserialize_credential(row['cookie'])
                invalid_reason = None
            except Exception as error:
                auth = None
                invalid_reason = (
                    'invalid_account_id'
                    if not ACCOUNT_ID_PATTERN.fullmatch(account_id)
                    else self._invalid_reason(error)
                )
            records.append(CredentialRecord(
                row_id=int(row['id']),
                account_id=account_id,
                created_at=row['create_time'],
                auth=auth,
                invalid_reason=invalid_reason,
            ))
        return records

    def insert(self, account_id: str, auth_or_payload: Any) -> CredentialRecord:
        """保存账号凭证；同一类型和账号已存在时更新最新记录。"""
        validate_account_id(account_id)
        serialized = serialize_credential(auth_or_payload)
        # 从待写入 Cookie 恢复，确保后续启动可以直接加载。
        auth = deserialize_credential(serialized)
        try:
            with self.engine.begin() as connection:
                dialect_name = connection.dialect.name
                type_column = self._case_sensitive(self.table.c.type, dialect_name)
                account_id_column = self._case_sensitive(
                    self.table.c.account_id,
                    dialect_name,
                )
                credential_type = (
                    self.credential_type.encode('utf-8')
                    if dialect_name in {'mysql', 'mariadb'}
                    else self.credential_type
                )
                stored_account_id = (
                    account_id.encode('utf-8')
                    if dialect_name in {'mysql', 'mariadb'}
                    else account_id
                )
                # 锁定最新匹配记录，避免同一账号连续扫码时重复新增。
                existing_id = connection.execute(
                    select(self.table.c.id)
                    .where(
                        self.table.c.project_id == self.project_id,
                        type_column == credential_type,
                        account_id_column == stored_account_id,
                    )
                    .order_by(self.table.c.id.desc())
                    .limit(1)
                    .with_for_update()
                ).scalar_one_or_none()

                if existing_id is None:
                    result = connection.execute(self.table.insert().values(
                        project_id=self.project_id,
                        type=self.credential_type,
                        account_id=account_id,
                        cookie=serialized,
                    ))
                    row_id = result.inserted_primary_key[0]
                    if row_id is None:
                        row_id = getattr(result, 'lastrowid', None)
                    if row_id is None:
                        raise CredentialStoreError('数据库未返回新凭证 ID')
                else:
                    row_id = existing_id
                    connection.execute(
                        self.table.update()
                        .where(self.table.c.id == row_id)
                        .values(
                            cookie=serialized,
                            create_time=func.current_timestamp(),
                        )
                    )
                created_at = connection.execute(
                    select(self.table.c.create_time).where(self.table.c.id == row_id)
                ).scalar_one_or_none()
        except CredentialStoreError:
            raise
        except SQLAlchemyError as error:
            raise CredentialStoreError('保存 MySQL 账号凭证失败') from error

        return CredentialRecord(
            row_id=int(row_id),
            account_id=account_id,
            created_at=created_at,
            auth=auth,
        )

    def close(self) -> None:
        """释放数据库连接池。"""
        self.engine.dispose()


__all__ = [
    'ACCOUNT_ID_PATTERN',
    'CREDENTIAL_TYPE',
    'CREDENTIAL_VERSION',
    'CredentialFormatError',
    'CredentialRecord',
    'CredentialStoreConfigurationError',
    'CredentialStoreError',
    'MySQLCredentialStore',
    'credential_payload_from_auth',
    'deserialize_credential',
    'normalize_credential_payload',
    'serialize_credential',
    'validate_account_id',
]

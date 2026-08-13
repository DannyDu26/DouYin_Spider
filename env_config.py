# coding=utf-8
"""按运行环境加载项目配置文件。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
VALID_APP_ENVS = frozenset({'dev', 'prod'})


class EnvironmentConfigurationError(RuntimeError):
    """环境配置不合法。"""


def _normalize_app_env(value: str | None) -> str:
    """校验环境名称，避免加载到未定义的配置文件。"""
    app_env = (value or '').strip()
    if app_env not in VALID_APP_ENVS:
        raise EnvironmentConfigurationError(
            "APP_ENV 仅支持 'dev' 或 'prod'，默认值为 'dev'"
        )
    return app_env


def get_app_env() -> str:
    """返回当前环境名称，仅允许 dev 或 prod。"""
    return _normalize_app_env(os.getenv('APP_ENV', 'dev'))


def _validate_env_file(env_file: Path, *, required: bool) -> bool:
    """检查配置文件路径，默认引导文件允许不存在。"""
    if not env_file.exists():
        if required:
            raise EnvironmentConfigurationError('APP_ENV_FILE 指定的环境文件不存在')
        return False
    if not env_file.is_file():
        raise EnvironmentConfigurationError(f'环境配置路径不是文件: {env_file}')
    return True


def _app_env_from_file(env_file: Path, *, required: bool) -> str | None:
    """只读取文件中的 APP_ENV，不把其他凭证写入进程环境。"""
    if not _validate_env_file(env_file, required=required):
        return None
    try:
        values = dotenv_values(dotenv_path=env_file, interpolate=False)
    except (OSError, UnicodeError) as error:
        raise EnvironmentConfigurationError('无法读取环境配置文件') from error
    if 'APP_ENV' not in values:
        return None
    return _normalize_app_env(values.get('APP_ENV'))


def load_environment(project_root: str | Path | None = None) -> Path | None:
    """先确定 dev/prod，再加载对应配置；系统变量始终优先。"""
    root = Path(project_root).resolve() if project_root is not None else PROJECT_ROOT

    raw_process_env = os.getenv('APP_ENV')
    override_value = os.getenv('APP_ENV_FILE')
    explicit_file = override_value is not None
    if explicit_file:
        override_value = override_value.strip()
        if not override_value:
            raise EnvironmentConfigurationError('APP_ENV_FILE 不能为空')
        configured_path = Path(override_value).expanduser()
        env_file = configured_path if configured_path.is_absolute() else root / configured_path
        env_file = env_file.resolve()
        if raw_process_env is None:
            file_app_env = _app_env_from_file(env_file, required=True)
        else:
            # 进程 APP_ENV 优先，但显式配置文件路径仍必须有效。
            _validate_env_file(env_file, required=True)
            file_app_env = None
    else:
        # .env 仅负责选择环境，数据库和 Cookie 等值不会被 API 加载。
        file_app_env = (
            _app_env_from_file(root / '.env', required=False)
            if raw_process_env is None
            else None
        )
        env_file = None

    app_env = (
        _normalize_app_env(raw_process_env)
        if raw_process_env is not None
        else (file_app_env or 'dev')
    )
    os.environ.setdefault('APP_ENV', app_env)

    if not explicit_file:
        env_file = root / f'.env.{app_env}'

    env_file = env_file.resolve()
    if not env_file.exists():
        # 配置文件可选，容器中可以完全依赖系统环境变量。
        return None
    if not env_file.is_file():
        raise EnvironmentConfigurationError(
            f'环境配置路径不是文件: {env_file}'
        )

    load_dotenv(dotenv_path=env_file, override=False)
    return env_file

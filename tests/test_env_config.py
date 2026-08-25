# coding=utf-8
"""多环境配置加载测试。"""

from pathlib import Path

import pytest

from app import env_config
from app.env_config import EnvironmentConfigurationError, get_app_env, load_environment


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """隔离宿主机和其他测试的环境选择配置。"""
    monkeypatch.delenv('APP_ENV', raising=False)
    monkeypatch.delenv('APP_ENV_FILE', raising=False)


def test_get_app_env_defaults_to_dev():
    assert get_app_env() == 'dev'


@pytest.mark.parametrize('value', ['dev', 'prod'])
def test_get_app_env_accepts_supported_values(monkeypatch, value):
    monkeypatch.setenv('APP_ENV', value)

    assert get_app_env() == value


@pytest.mark.parametrize('value', ['', 'development', 'production', 'DEV', 'test'])
def test_get_app_env_rejects_unsupported_values(monkeypatch, value):
    monkeypatch.setenv('APP_ENV', value)

    with pytest.raises(EnvironmentConfigurationError, match='APP_ENV'):
        get_app_env()


def test_load_environment_uses_dev_file_by_default(monkeypatch, tmp_path):
    env_file = tmp_path / '.env.dev'
    env_file.write_text('SERVICE_NAME=dev-service\n', encoding='utf-8')
    monkeypatch.delenv('SERVICE_NAME', raising=False)

    loaded_path = load_environment(tmp_path)

    assert loaded_path == env_file.resolve()
    assert env_config.os.environ['SERVICE_NAME'] == 'dev-service'


def test_load_environment_uses_prod_file(monkeypatch, tmp_path):
    (tmp_path / '.env.dev').write_text('SERVICE_NAME=dev-service\n', encoding='utf-8')
    prod_file = tmp_path / '.env.prod'
    prod_file.write_text('SERVICE_NAME=prod-service\n', encoding='utf-8')
    monkeypatch.setenv('APP_ENV', 'prod')
    monkeypatch.delenv('SERVICE_NAME', raising=False)

    loaded_path = load_environment(tmp_path)

    assert loaded_path == prod_file.resolve()
    assert env_config.os.environ['SERVICE_NAME'] == 'prod-service'


def test_dotenv_bootstrap_selects_prod_without_loading_other_values(monkeypatch, tmp_path):
    (tmp_path / '.env').write_text(
        'APP_ENV=prod\nBOOTSTRAP_SECRET=must-not-load\n',
        encoding='utf-8',
    )
    prod_file = tmp_path / '.env.prod'
    prod_file.write_text('SERVICE_NAME=prod-service\n', encoding='utf-8')
    monkeypatch.delenv('SERVICE_NAME', raising=False)
    monkeypatch.delenv('BOOTSTRAP_SECRET', raising=False)

    loaded_path = load_environment(tmp_path)

    assert loaded_path == prod_file.resolve()
    assert get_app_env() == 'prod'
    assert env_config.os.environ['SERVICE_NAME'] == 'prod-service'
    assert 'BOOTSTRAP_SECRET' not in env_config.os.environ


def test_process_app_env_overrides_dotenv_bootstrap(monkeypatch, tmp_path):
    (tmp_path / '.env').write_text('APP_ENV=prod\n', encoding='utf-8')
    dev_file = tmp_path / '.env.dev'
    dev_file.write_text('SERVICE_NAME=dev-service\n', encoding='utf-8')
    monkeypatch.setenv('APP_ENV', 'dev')
    monkeypatch.delenv('SERVICE_NAME', raising=False)

    assert load_environment(tmp_path) == dev_file.resolve()
    assert env_config.os.environ['SERVICE_NAME'] == 'dev-service'


def test_invalid_app_env_in_dotenv_bootstrap_is_rejected(tmp_path):
    (tmp_path / '.env').write_text('APP_ENV=staging\n', encoding='utf-8')

    with pytest.raises(EnvironmentConfigurationError, match='APP_ENV'):
        load_environment(tmp_path)


def test_explicit_relative_file_overrides_default(monkeypatch, tmp_path):
    custom_dir = tmp_path / 'config'
    custom_dir.mkdir()
    custom_file = custom_dir / 'local.env'
    custom_file.write_text('SERVICE_NAME=custom-service\n', encoding='utf-8')
    (tmp_path / '.env.dev').write_text('SERVICE_NAME=dev-service\n', encoding='utf-8')
    monkeypatch.setenv('APP_ENV_FILE', 'config/local.env')
    monkeypatch.delenv('SERVICE_NAME', raising=False)

    loaded_path = load_environment(tmp_path)

    assert loaded_path == custom_file.resolve()
    assert env_config.os.environ['SERVICE_NAME'] == 'custom-service'


def test_explicit_absolute_file_is_supported(monkeypatch, tmp_path):
    custom_file = tmp_path / 'external.env'
    custom_file.write_text('SERVICE_NAME=absolute-service\n', encoding='utf-8')
    monkeypatch.setenv('APP_ENV_FILE', str(custom_file))
    monkeypatch.delenv('SERVICE_NAME', raising=False)

    assert load_environment(tmp_path / 'unused') == custom_file.resolve()
    assert env_config.os.environ['SERVICE_NAME'] == 'absolute-service'


def test_explicit_file_can_define_environment(monkeypatch, tmp_path):
    custom_file = tmp_path / 'custom.env'
    custom_file.write_text('APP_ENV=prod\nSERVICE_NAME=custom-prod\n', encoding='utf-8')
    monkeypatch.setenv('APP_ENV_FILE', str(custom_file))
    monkeypatch.delenv('SERVICE_NAME', raising=False)

    assert load_environment(tmp_path) == custom_file.resolve()
    assert get_app_env() == 'prod'
    assert env_config.os.environ['SERVICE_NAME'] == 'custom-prod'


def test_process_environment_overrides_explicit_file_environment(monkeypatch, tmp_path):
    custom_file = tmp_path / 'custom.env'
    custom_file.write_text('APP_ENV=invalid\nSERVICE_NAME=custom-dev\n', encoding='utf-8')
    monkeypatch.setenv('APP_ENV', 'dev')
    monkeypatch.setenv('APP_ENV_FILE', str(custom_file))
    monkeypatch.delenv('SERVICE_NAME', raising=False)

    assert load_environment(tmp_path) == custom_file.resolve()
    assert get_app_env() == 'dev'
    assert env_config.os.environ['SERVICE_NAME'] == 'custom-dev'


def test_system_environment_has_priority(monkeypatch, tmp_path):
    env_file = tmp_path / '.env.dev'
    env_file.write_text('SERVICE_NAME=file-service\n', encoding='utf-8')
    monkeypatch.setenv('SERVICE_NAME', 'system-service')

    load_environment(tmp_path)

    assert env_config.os.environ['SERVICE_NAME'] == 'system-service'


def test_missing_file_continues_with_system_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('SERVICE_NAME', 'system-service')

    assert load_environment(tmp_path) is None
    assert env_config.os.environ['SERVICE_NAME'] == 'system-service'


def test_explicit_missing_file_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv('APP_ENV_FILE', 'config/missing.env')

    with pytest.raises(EnvironmentConfigurationError, match='不存在'):
        load_environment(tmp_path)


def test_empty_explicit_file_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv('APP_ENV_FILE', '   ')

    with pytest.raises(EnvironmentConfigurationError, match='APP_ENV_FILE'):
        load_environment(tmp_path)


def test_directory_path_is_rejected(monkeypatch, tmp_path):
    config_dir = tmp_path / 'config-dir'
    config_dir.mkdir()
    monkeypatch.setenv('APP_ENV_FILE', str(config_dir))

    with pytest.raises(EnvironmentConfigurationError, match='不是文件'):
        load_environment(tmp_path)


def test_load_dotenv_is_called_without_overriding(monkeypatch, tmp_path):
    env_file = tmp_path / '.env.dev'
    env_file.write_text('SERVICE_NAME=dev-service\n', encoding='utf-8')
    captured = {}

    def fake_load_dotenv(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(env_config, 'load_dotenv', fake_load_dotenv)

    load_environment(tmp_path)

    assert captured == {'dotenv_path': env_file.resolve(), 'override': False}


def test_default_root_is_module_project_root(monkeypatch, tmp_path):
    env_file = tmp_path / '.env.dev'
    env_file.write_text('SERVICE_NAME=root-service\n', encoding='utf-8')
    monkeypatch.setattr(env_config, 'PROJECT_ROOT', tmp_path)
    monkeypatch.delenv('SERVICE_NAME', raising=False)

    assert load_environment() == env_file.resolve()
    assert env_config.os.environ['SERVICE_NAME'] == 'root-service'


def test_environment_file_cannot_switch_selected_environment(monkeypatch, tmp_path):
    env_file = tmp_path / '.env.dev'
    env_file.write_text('APP_ENV=prod\nSERVICE_NAME=dev-service\n', encoding='utf-8')

    load_environment(tmp_path)

    assert get_app_env() == 'dev'
    assert env_config.os.environ['SERVICE_NAME'] == 'dev-service'

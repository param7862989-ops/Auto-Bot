from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from universal_ai_tools.config import (
    ENV_ENABLE_WRITE_TOOLS,
    ENV_LOG_LEVEL,
    ENV_WORKSPACE,
    REMOVED_SETTINGS,
    AppConfig,
    ConfigError,
    load_config,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "users" / "me"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def workspace(home: Path) -> Path:
    path = home / "projects" / "demo"
    path.mkdir(parents=True)
    return path


def test_valid_workspace_is_resolved(workspace: Path, home: Path) -> None:
    config = load_config({ENV_WORKSPACE: str(workspace)}, home=home)

    assert config.workspace_root == workspace.resolve()
    assert config.workspace_root.is_absolute()
    assert config.log_level == "INFO"


def test_workspace_value_is_trimmed(workspace: Path, home: Path) -> None:
    config = load_config({ENV_WORKSPACE: f"  {workspace}  "}, home=home)

    assert config.workspace_root == workspace.resolve()


def test_log_level_is_normalized(workspace: Path, home: Path) -> None:
    config = load_config({ENV_WORKSPACE: str(workspace), ENV_LOG_LEVEL: " debug "}, home=home)

    assert config.log_level == "DEBUG"


def test_reads_os_environ_by_default(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, home: Path
) -> None:
    monkeypatch.setenv(ENV_WORKSPACE, str(workspace))
    monkeypatch.setenv(ENV_LOG_LEVEL, "WARNING")

    config = load_config(home=home)

    assert config.workspace_root == workspace.resolve()
    assert config.log_level == "WARNING"


def test_only_allowlisted_variables_are_read(workspace: Path, home: Path) -> None:
    sentinel = "sk-do-not-leak-0123456789"
    environ = {
        ENV_WORKSPACE: str(workspace),
        "ANTHROPIC_API_KEY": sentinel,
        "UNIVERSAL_AI_TOOLS_UNKNOWN": sentinel,
    }

    config = load_config(environ, home=home)

    assert set(config.model_dump()) == {"workspace_root", "log_level", "enable_write_tools"}
    assert sentinel not in repr(config)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_workspace_fails_clearly(value: str | None, home: Path) -> None:
    environ = {} if value is None else {ENV_WORKSPACE: value}

    with pytest.raises(ConfigError, match=f"{ENV_WORKSPACE} is not set"):
        load_config(environ, home=home)


def test_relative_workspace_is_rejected(home: Path) -> None:
    with pytest.raises(ConfigError, match="must be an absolute path") as excinfo:
        load_config({ENV_WORKSPACE: "projects/demo"}, home=home)

    assert ENV_WORKSPACE in str(excinfo.value)


def test_nonexistent_workspace_is_rejected(home: Path) -> None:
    missing = home / "does-not-exist"

    with pytest.raises(ConfigError, match="directory does not exist"):
        load_config({ENV_WORKSPACE: str(missing)}, home=home)


def test_file_workspace_is_rejected(home: Path) -> None:
    file_path = home / "notes.txt"
    file_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ConfigError, match="is not a directory"):
        load_config({ENV_WORKSPACE: str(file_path)}, home=home)


def test_home_directory_is_rejected(home: Path) -> None:
    with pytest.raises(ConfigError, match="must not be the home directory"):
        load_config({ENV_WORKSPACE: str(home)}, home=home)


def test_parent_of_home_is_rejected(home: Path) -> None:
    with pytest.raises(ConfigError, match="must not be the home directory"):
        load_config({ENV_WORKSPACE: str(home.parent)}, home=home)


def test_filesystem_root_is_rejected(home: Path) -> None:
    root = Path(home.anchor)

    with pytest.raises(ConfigError, match="must not be a filesystem or drive root"):
        load_config({ENV_WORKSPACE: str(root)}, home=home)


def test_real_home_directory_is_rejected_by_default() -> None:
    with pytest.raises(ConfigError, match="must not be the home directory"):
        load_config({ENV_WORKSPACE: str(Path.home())})


def test_invalid_log_level_is_rejected(workspace: Path, home: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config({ENV_WORKSPACE: str(workspace), ENV_LOG_LEVEL: "verbose"}, home=home)

    assert ENV_LOG_LEVEL in str(excinfo.value)


def test_config_is_immutable(workspace: Path, home: Path) -> None:
    config = load_config({ENV_WORKSPACE: str(workspace)}, home=home)

    with pytest.raises(ValidationError):
        config.workspace_root = home  # type: ignore[misc]


def test_unknown_fields_are_rejected(workspace: Path) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"workspace_root": str(workspace), "allow_shell": True})


# Write tools opt-in


def test_write_tools_are_disabled_when_unset(workspace: Path, home: Path) -> None:
    config = load_config({ENV_WORKSPACE: str(workspace)}, home=home)

    assert config.enable_write_tools is False


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_empty_write_tools_setting_is_treated_as_unset(
    workspace: Path, home: Path, value: str
) -> None:
    config = load_config({ENV_WORKSPACE: str(workspace), ENV_ENABLE_WRITE_TOOLS: value}, home=home)

    assert config.enable_write_tools is False


@pytest.mark.parametrize("value", ["false", "False", "FALSE", " false ", "\tFaLsE\n"])
def test_explicit_false_disables_write_tools(workspace: Path, home: Path, value: str) -> None:
    config = load_config({ENV_WORKSPACE: str(workspace), ENV_ENABLE_WRITE_TOOLS: value}, home=home)

    assert config.enable_write_tools is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", " true ", "\ttRuE\n"])
def test_explicit_true_enables_write_tools(workspace: Path, home: Path, value: str) -> None:
    config = load_config({ENV_WORKSPACE: str(workspace), ENV_ENABLE_WRITE_TOOLS: value}, home=home)

    assert config.enable_write_tools is True


@pytest.mark.parametrize(
    "value",
    ["1", "0", "yes", "no", "on", "off", "y", "t", "enabled", "truee", "true false", "'true'"],
)
def test_invalid_write_tools_setting_is_rejected(workspace: Path, home: Path, value: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config({ENV_WORKSPACE: str(workspace), ENV_ENABLE_WRITE_TOOLS: value}, home=home)

    assert str(excinfo.value) == (
        f"Invalid configuration:\n  {ENV_ENABLE_WRITE_TOOLS}: "
        "must be 'true' or 'false' (case-insensitive)"
    )


def test_invalid_write_tools_setting_is_not_echoed(workspace: Path, home: Path) -> None:
    sentinel = "sk-do-not-echo-0123456789"

    with pytest.raises(ConfigError) as excinfo:
        load_config({ENV_WORKSPACE: str(workspace), ENV_ENABLE_WRITE_TOOLS: sentinel}, home=home)

    assert sentinel not in str(excinfo.value)


def test_write_tools_setting_is_read_from_os_environ(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, home: Path
) -> None:
    monkeypatch.setenv(ENV_WORKSPACE, str(workspace))
    monkeypatch.setenv(ENV_ENABLE_WRITE_TOOLS, "TRUE")

    assert load_config(home=home).enable_write_tools is True


def test_write_tools_setting_does_not_change_other_settings(workspace: Path, home: Path) -> None:
    base = {ENV_WORKSPACE: str(workspace), ENV_LOG_LEVEL: "debug"}

    disabled = load_config(base, home=home)
    enabled = load_config({**base, ENV_ENABLE_WRITE_TOOLS: "true"}, home=home)

    assert enabled.workspace_root == disabled.workspace_root == workspace.resolve()
    assert enabled.log_level == disabled.log_level == "DEBUG"


@pytest.mark.parametrize("value", [1, 0, "yes", None])
def test_app_config_accepts_only_booleans_for_write_tools(workspace: Path, value: object) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"workspace_root": str(workspace), "enable_write_tools": value})

    enabled = AppConfig.model_validate(
        {"workspace_root": str(workspace), "enable_write_tools": True}
    )
    assert enabled.enable_write_tools is True


# Removed command execution settings


def test_removed_settings_are_the_two_command_variables() -> None:
    assert REMOVED_SETTINGS == (
        "UNIVERSAL_AI_TOOLS_ENABLE_COMMAND_TOOLS",
        "UNIVERSAL_AI_TOOLS_COMMAND_ALLOWLIST",
    )


@pytest.mark.parametrize("setting", REMOVED_SETTINGS)
@pytest.mark.parametrize(
    "value", ["true", "false", "pytest", "git", "mypy", "ruff", "pytest,git", "anything"]
)
def test_any_value_of_a_removed_setting_stops_the_server(
    workspace: Path, home: Path, setting: str, value: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config({ENV_WORKSPACE: str(workspace), setting: value}, home=home)

    assert str(excinfo.value) == (
        f"Invalid configuration:\n  {setting}: command execution was removed and cannot "
        "be enabled; unset this variable"
    )


def test_both_removed_settings_are_reported_together(workspace: Path, home: Path) -> None:
    environ = {ENV_WORKSPACE: str(workspace), **dict.fromkeys(REMOVED_SETTINGS, "true")}

    with pytest.raises(ConfigError) as excinfo:
        load_config(environ, home=home)

    for setting in REMOVED_SETTINGS:
        assert setting in str(excinfo.value)


@pytest.mark.parametrize("setting", REMOVED_SETTINGS)
def test_removed_setting_is_refused_from_os_environ(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, home: Path, setting: str
) -> None:
    monkeypatch.setenv(ENV_WORKSPACE, str(workspace))
    monkeypatch.setenv(setting, "true")

    with pytest.raises(ConfigError, match="command execution was removed"):
        load_config(home=home)


@pytest.mark.parametrize("setting", REMOVED_SETTINGS)
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_removed_setting_counts_as_unset(
    workspace: Path, home: Path, setting: str, value: str
) -> None:
    config = load_config({ENV_WORKSPACE: str(workspace), setting: value}, home=home)

    assert config.workspace_root == workspace.resolve()


@pytest.mark.parametrize("setting", REMOVED_SETTINGS)
def test_removed_setting_value_is_not_echoed(workspace: Path, home: Path, setting: str) -> None:
    sentinel = "sk-do-not-echo-0123456789"

    with pytest.raises(ConfigError) as excinfo:
        load_config({ENV_WORKSPACE: str(workspace), setting: sentinel}, home=home)

    assert sentinel not in str(excinfo.value)


def test_env_example_mentions_no_command_execution() -> None:
    example = (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8")

    assert "run_command" not in example
    assert "COMMAND" not in example
    assert "UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS=false" in example


@pytest.mark.parametrize(
    "field", [("enable_command_tools", True), ("command_allowlist", ("pytest", "git"))]
)
def test_app_config_has_no_command_settings(workspace: Path, field: tuple[str, object]) -> None:
    name, value = field

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"workspace_root": str(workspace), name: value})

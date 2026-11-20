"""The config file shared by both clients, and environment mode."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from service_support import service, write_config
from universal_ai_tools.config import (
    ENV_ENABLE_WRITE_TOOLS,
    ENV_LOG_LEVEL,
    ENV_WORKSPACE,
    REMOVED_AGENT_SETTINGS,
    ConfigError,
)
from universal_ai_tools.settings import (
    ENV_CONFIG,
    ConfigFile,
    load_service_config,
    render_config,
    service_config_from_data,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = (tmp_path / "projects" / "demo").resolve()
    path.mkdir(parents=True)
    return path


def test_defaults_enable_nothing_but_reading(workspace: Path, tmp_path: Path) -> None:
    config = service(tmp_path / "state", workspace)

    assert config.file_mode
    assert config.workspace_root == workspace
    assert config.app.enable_write_tools is False
    assert not (config.terminal.enabled)
    assert not (config.screen.enabled)
    assert not (config.input.enabled)
    assert config.filesystem.approval == "every_call"
    assert config.approval.channel == "auto"
    assert config.audit_path == (tmp_path / "state").resolve() / "audit.jsonl"


def test_capabilities_are_read(workspace: Path, tmp_path: Path) -> None:
    config = service(
        tmp_path / "state",
        workspace,
        {
            "filesystem": {"write": True, "approval": "once_per_session"},
            "terminal": {"programs": ["python"]},
            "screen": {"enabled": True},
            "input": {"enabled": True, "allowed_processes": ["notepad.exe"]},
        },
    )

    assert config.app.enable_write_tools is True
    assert config.filesystem.approval == "once_per_session"
    assert config.terminal.programs == ("python",)
    assert config.input.allowed_processes == ("notepad.exe",)


@pytest.mark.parametrize(
    ("sections", "message"),
    [
        ({"filesystem": {"wirte": True}}, "wirte"),
        ({"filesystem": {"write": "yes"}}, "filesystem.write"),
        ({"terminal": {"enabled": 1}}, "terminal.enabled"),
        ({"approval": {"channel": "model"}}, "approval.channel"),
        ({"approval": {"timeout_seconds": 1}}, "approval.timeout_seconds"),
        ({"screen": {"approval": "never"}}, "screen.approval"),
        (
            {"terminal": {"default_timeout_seconds": 900, "max_timeout_seconds": 60}},
            "must not exceed",
        ),
        ({"surprise": {"x": 1}}, "surprise"),
    ],
)
def test_invalid_files_are_refused(
    workspace: Path, tmp_path: Path, sections: dict[str, dict[str, object]], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        service(tmp_path / "state", workspace, sections)


def test_there_is_no_never_ask_setting(workspace: Path, tmp_path: Path) -> None:
    for policy in ("never", "none", "auto"):
        with pytest.raises(ConfigError):
            service(tmp_path / "state", workspace, {"filesystem": {"approval": policy}})


def test_the_config_must_not_be_inside_the_workspace(workspace: Path) -> None:
    with pytest.raises(ConfigError, match="config file is inside the workspace"):
        service(workspace / ".settings", workspace)


def test_the_audit_log_must_not_be_inside_the_workspace(workspace: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="audit log is inside the workspace"):
        service(tmp_path / "state", workspace, {"audit": {"path": str(workspace / "audit.jsonl")}})


def test_the_workspace_must_not_hold_claude_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    settings = home / ".claude"
    settings.mkdir(parents=True)
    data = tomllib.loads(f"version = 1\nworkspace = '{settings}'\n")

    with pytest.raises(ConfigError, match="contains Claude's settings"):
        service_config_from_data(data, tmp_path / "state" / "config.toml", home=home)


def test_broad_workspaces_are_still_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="home directory"):
        service(tmp_path / "state", Path.home())


def test_render_round_trips(workspace: Path, tmp_path: Path) -> None:
    original = ConfigFile.model_validate(
        {
            "version": 1,
            "workspace": str(workspace),
            "terminal": {"enabled": True, "programs": ["python", "C:\\it's\\tool.exe"]},
            "input": {"allowed_processes": ["notepad.exe"]},
        }
    )

    rendered = render_config(original)

    assert ConfigFile.model_validate(tomllib.loads(rendered)) == original


# Choosing the mode


def test_environment_mode_is_read_only(workspace: Path) -> None:
    config = load_service_config({ENV_WORKSPACE: str(workspace)})

    assert config.source == "environment"
    assert config.app.enable_write_tools is False
    assert config.audit_path is None


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_environment_mode_cannot_enable_writes(workspace: Path, value: str) -> None:
    """No approval and no audit log in environment mode, so no tool that changes files."""
    with pytest.raises(ConfigError, match="environment mode has no approval and no audit log"):
        load_service_config({ENV_WORKSPACE: str(workspace), ENV_ENABLE_WRITE_TOOLS: value})


def test_the_terminal_cannot_be_enabled(workspace: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="disabled for safety"):
        service(
            tmp_path / "state",
            workspace,
            {"terminal": {"enabled": True, "programs": ["python"]}},
        )


def test_file_mode_from_the_environment(workspace: Path, tmp_path: Path) -> None:
    path = write_config(tmp_path / "state", workspace)

    config = load_service_config({ENV_CONFIG: str(path)})

    assert config.file_mode
    assert config.config_path == path.resolve()


@pytest.mark.parametrize("other", [ENV_WORKSPACE, ENV_LOG_LEVEL, ENV_ENABLE_WRITE_TOOLS])
def test_file_and_variables_together_are_refused(
    workspace: Path, tmp_path: Path, other: str
) -> None:
    path = write_config(tmp_path / "state", workspace)

    with pytest.raises(ConfigError, match=f"unset {other}"):
        load_service_config({ENV_CONFIG: str(path), other: "true"})


def test_missing_config_points_to_setup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="universal-ai-tools setup"):
        load_service_config({ENV_CONFIG: str(tmp_path / "missing.toml")})
    with pytest.raises(ConfigError, match="universal-ai-tools setup"):
        load_service_config({})


@pytest.mark.parametrize("setting", REMOVED_AGENT_SETTINGS)
def test_api_agent_settings_are_refused_without_echo(workspace: Path, setting: str) -> None:
    secret = "sk-ant-do-not-echo-0123456789"  # noqa: S105 - a marker, not a password

    with pytest.raises(ConfigError) as excinfo:
        load_service_config({ENV_WORKSPACE: str(workspace), setting: secret})

    assert setting in str(excinfo.value)
    assert "Claude Desktop or Claude Code is the orchestrator" in str(excinfo.value)
    assert secret not in str(excinfo.value)

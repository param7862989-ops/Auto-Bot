"""Registering with Claude Desktop and Claude Code, and the setup command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from universal_ai_tools import cli, clients
from universal_ai_tools.settings import ENV_CONFIG


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "config.toml"


@pytest.fixture
def entry(config_path: Path) -> dict[str, object]:
    return clients.server_entry(config_path, python="C:\\py\\python.exe")


def test_the_entry_carries_only_the_config_path(
    entry: dict[str, object], config_path: Path
) -> None:
    assert entry == {
        "command": "C:\\py\\python.exe",
        "args": ["-I", "-m", "universal_ai_tools.server"],
        "env": {ENV_CONFIG: str(config_path)},
    }


def test_desktop_plan_keeps_every_other_setting(tmp_path: Path, entry: dict[str, object]) -> None:
    path = tmp_path / "claude_desktop_config.json"
    original = {
        "preferences": {"sidebarMode": "chat", "nested": [1, 2, {"x": None}]},
        "mcpServers": {"other": {"command": "node", "args": ["x.js"], "env": {"TOKEN": "t"}}},
        "unicode": "héllo",
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    plan = clients.plan_desktop(path, entry)
    backup = clients.apply_desktop(plan)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert plan.action == "add"
    assert written["preferences"] == original["preferences"]
    assert written["unicode"] == "héllo"
    assert written["mcpServers"]["other"] == original["mcpServers"]["other"]  # type: ignore[index]
    assert written["mcpServers"][clients.SERVER_NAME] == entry
    assert backup is not None
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert "+" in plan.diff()
    assert clients.SERVER_NAME in plan.diff()


def test_desktop_plan_is_idempotent_and_replaces_stale_entries(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    path = tmp_path / "claude_desktop_config.json"
    stale = {"mcpServers": {clients.SERVER_NAME: {"command": "old", "args": []}}}
    path.write_text(json.dumps(stale), encoding="utf-8")

    first = clients.plan_desktop(path, entry)
    clients.apply_desktop(first)
    second = clients.plan_desktop(path, entry)

    assert first.action == "replace"
    assert first.previous_entry == {"command": "old", "args": []}
    assert second.action == "unchanged"
    assert clients.apply_desktop(second) is None


def test_a_missing_desktop_config_is_created(tmp_path: Path, entry: dict[str, object]) -> None:
    path = tmp_path / "Claude" / "claude_desktop_config.json"

    plan = clients.plan_desktop(path, entry)
    assert clients.apply_desktop(plan) is None

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "mcpServers": {clients.SERVER_NAME: entry}
    }


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"mcpServers": []}'])
def test_unusable_desktop_configs_are_never_overwritten(
    tmp_path: Path, entry: dict[str, object], content: str
) -> None:
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(clients.ClientConfigError):
        clients.plan_desktop(path, entry)
    assert path.read_text(encoding="utf-8") == content


def test_a_desktop_config_changed_after_planning_is_not_written(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    path = tmp_path / "claude_desktop_config.json"
    path.write_text("{}", encoding="utf-8")
    plan = clients.plan_desktop(path, entry)
    path.write_text('{"changed": true}', encoding="utf-8")

    with pytest.raises(clients.ClientConfigError, match="changed since"):
        clients.apply_desktop(plan)
    assert json.loads(path.read_text(encoding="utf-8")) == {"changed": True}


def test_the_store_build_location_is_preferred(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    store = local / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    (tmp_path / "Roaming" / "Claude").mkdir(parents=True)

    found = clients.desktop_config_path(
        {"LOCALAPPDATA": str(local), "APPDATA": str(tmp_path / "Roaming")}
    )

    assert found == store / "claude_desktop_config.json"


def test_code_registrations_are_read_not_written(tmp_path: Path, entry: dict[str, object]) -> None:
    settings = {
        "mcpServers": {clients.SERVER_NAME: entry},
        "projects": {
            "C:/work/a": {"mcpServers": {clients.SERVER_NAME: {"command": "old"}}},
            "C:/work/b": {"mcpServers": {}},
        },
    }
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    before = path.read_bytes()

    found = clients.code_registrations(tmp_path)

    assert clients.same_registration(found.user, entry)
    assert list(found.local) == ["C:/work/a"]
    assert not clients.same_registration(found.local["C:/work/a"], entry)
    assert path.read_bytes() == before


def test_code_commands_use_the_cli(entry: dict[str, object]) -> None:
    (add,) = clients.code_add_commands(entry, replace=False)
    assert add[1:6] == ["mcp", "add-json", "--scope", "user", clients.SERVER_NAME]
    assert json.loads(add[6]) == {"type": "stdio", **entry}
    remove, _add_again = clients.code_add_commands(entry, replace=True)
    assert remove[1:] == ["mcp", "remove", "--scope", "user", clients.SERVER_NAME]


# setup


def test_setup_dry_run_changes_nothing(
    tmp_path: Path, config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            [
                "setup",
                "--workspace",
                str(workspace),
                "--config",
                str(config_path),
                "--enable",
                "filesystem-write",
                "--clients",
                "",
                "--dry-run",
            ]
        )

    out = capsys.readouterr().out
    assert exit_info.value.code == 0  # the dry run shows every step, then stops
    assert "dry run: not applied" in out
    assert "write = true" in out
    assert not config_path.exists()


def test_setup_writes_a_valid_config(
    tmp_path: Path, config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from universal_ai_tools.settings import load_config_file

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            [
                "setup",
                "--workspace",
                str(workspace),
                "--config",
                str(config_path),
                "--enable",
                "screen",
                "--clients",
                "",
                "--yes",
            ]
        )

    assert exit_info.value.code == 0
    loaded = load_config_file(config_path)
    assert loaded.screen.enabled
    assert not loaded.terminal.enabled
    assert loaded.workspace_root == workspace.resolve()

    with pytest.raises(SystemExit):
        cli.main(["config", "--config", str(config_path), "enable", "filesystem-write"])
    assert load_config_file(config_path).app.enable_write_tools is True
    assert (config_path.parent / "config.toml.bak").exists()


def test_setup_refuses_an_unknown_capability(
    tmp_path: Path, config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            [
                "setup",
                "--workspace",
                str(tmp_path),
                "--config",
                str(config_path),
                "--enable",
                "everything",
                "--clients",
                "",
            ]
        )

    assert exit_info.value.code == 2
    assert "unknown capability" in capsys.readouterr().err

"""setup and doctor, against fake clients in temporary directories only.

Every test points Claude Desktop's config file, Claude Code's ``~/.claude.json``, and the
``claude`` command at stand-ins under ``tmp_path`` (the ``clients`` fixture), so nothing here
can read or change the real client configurations.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mcp.client.stdio  # noqa: F401 - see below
import pytest

from universal_ai_tools import cli, clients
from universal_ai_tools.audit import read_records
from universal_ai_tools.settings import load_config_file, read_config_file

# mcp.client.stdio binds sys.stderr as a default argument when first imported. Imported here,
# at collection, it gets the real stderr, as in the other stdio tests; imported first inside
# a test, it would get pytest's capture object, which has no file descriptor.

OTHER_SERVER = {"command": "node", "args": ["other.js"], "env": {"TOKEN": "keep-me"}}


@dataclass
class FakeClients:
    desktop: Path
    code_settings: Path
    commands: list[tuple[list[str], Path | None]] = field(default_factory=list)

    @property
    def user_settings(self) -> Path:
        return self.code_settings.parent / ".claude" / "settings.json"

    @property
    def runtime(self) -> Path:
        return self.code_settings.parent / ".universal-ai-tools" / "runtime"


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClients:
    desktop = tmp_path / "Claude" / "claude_desktop_config.json"
    desktop.parent.mkdir()
    desktop.write_text(
        json.dumps({"preferences": {"theme": "dark"}, "mcpServers": {"other": OTHER_SERVER}}),
        encoding="utf-8",
    )
    code_settings = tmp_path / "home" / ".claude.json"
    code_settings.parent.mkdir()
    code_settings.write_text(json.dumps({"numStartups": 3}), encoding="utf-8")
    state = FakeClients(desktop, code_settings)

    def run(command: list[str], cwd: Path | None = None) -> tuple[int, str]:
        state.commands.append((command, cwd))
        return 0, "ok"

    monkeypatch.setattr(clients, "desktop_config_path", lambda environ=None: desktop)
    monkeypatch.setattr(clients, "claude_code_settings_path", lambda home=None: code_settings)
    monkeypatch.setattr(clients, "claude_cli", lambda: "claude")
    monkeypatch.setattr(clients, "run_claude", run)
    # The guard's hooks and the runtime: also stand-ins, never the real ones.
    monkeypatch.setattr(clients, "claude_user_settings_path", lambda home=None: state.user_settings)
    monkeypatch.setattr(clients, "runtime_directory", lambda home=None: state.runtime)
    return state


def setup(*arguments: str) -> int:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["setup", *arguments])
    code = exit_info.value.code
    return code if isinstance(code, int) else 1


def snapshot(*paths: Path) -> dict[str, bytes | None]:
    found: dict[str, bytes | None] = {}
    for root in paths:
        for path in [root, *root.rglob("*")] if root.is_dir() else [root]:
            found[str(path)] = path.read_bytes() if path.is_file() else None
    return found


# Preparing a proposal changes nothing.


def test_a_dry_run_shows_every_step_and_writes_nothing(
    tmp_path: Path, fake: FakeClients, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "scratch"
    config = tmp_path / "state" / "config.toml"
    before = snapshot(fake.desktop.parent, fake.code_settings.parent)

    code = setup(
        "--workspace", str(workspace), "--create-workspace", "--config", str(config),
        "--enable", "filesystem-write", "--dry-run",
    )  # fmt: skip

    out = capsys.readouterr().out
    assert code == 0
    assert not workspace.exists()
    assert not config.parent.exists()
    assert snapshot(fake.desktop.parent, fake.code_settings.parent) == before
    assert fake.commands == []
    for step in ("[0/4] Workspace", "[1/4] Config file", "[2/4] Audit log"):
        assert step in out
    assert "[3/4] Claude Desktop" in out
    assert '"-I"' in out  # the Desktop diff shows the isolated launch
    assert "claude mcp add-json --scope user universal-ai-tools" in out.replace('"', "")
    assert "Nothing here auto-approves any tool" in out


# Setting up.


def test_setup_creates_the_scratch_workspace_config_and_audit_log(
    tmp_path: Path, fake: FakeClients
) -> None:
    workspace = tmp_path / "scratch"
    config = tmp_path / "state" / "config.toml"

    code = setup(
        "--workspace", str(workspace), "--create-workspace", "--config", str(config),
        "--enable", "filesystem-write", "--clients", "", "--yes",
    )  # fmt: skip

    assert code == 0
    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []
    service = load_config_file(config)
    assert service.workspace_root == workspace.resolve()
    assert service.app.enable_write_tools is True
    assert service.screen.enabled is False
    assert service.input.enabled is False
    assert service.terminal.enabled is False
    assert service.filesystem.approval == "every_call"
    (record,) = read_records(config.parent / "audit.jsonl")
    assert record["event"] == "setup"


def test_a_missing_workspace_is_created_only_when_asked(
    tmp_path: Path, fake: FakeClients, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "scratch"

    code = setup(
        "--workspace", str(workspace), "--config", str(tmp_path / "state" / "config.toml"),
        "--clients", "", "--yes",
    )  # fmt: skip

    assert code == 2
    assert "--create-workspace" in capsys.readouterr().err
    assert not workspace.exists()


@pytest.mark.parametrize("where", ["home", "home-parent", "config-inside"])
def test_a_planned_workspace_is_checked_before_it_is_created(
    tmp_path: Path,
    fake: FakeClients,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    where: str,
) -> None:
    """None of these directories exists yet, so only the planned-workspace check can refuse."""
    home = tmp_path / "users" / "me"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config = tmp_path / "state" / "config.toml"
    workspace = {
        "home": home,
        "home-parent": tmp_path / "users",
        "config-inside": tmp_path / "planned",
    }[where]
    if where == "config-inside":
        config = workspace / "state" / "config.toml"

    code = setup(
        "--workspace", str(workspace), "--create-workspace", "--config", str(config),
        "--clients", "", "--yes",
    )  # fmt: skip

    assert code == 2
    assert not workspace.exists()
    assert not config.exists()
    assert "error" in capsys.readouterr().err


def test_running_setup_again_keeps_every_setting(tmp_path: Path, fake: FakeClients) -> None:
    workspace = tmp_path / "scratch"
    config = tmp_path / "state" / "config.toml"
    assert setup(
        "--workspace", str(workspace), "--create-workspace", "--config", str(config),
        "--enable", "filesystem-write", "--clients", "", "--yes",
    ) == 0  # fmt: skip
    # The user edits the file by hand.
    text = config.read_text(encoding="utf-8").replace(
        "timeout_seconds = 120", "timeout_seconds = 300"
    )
    config.write_text(text, encoding="utf-8")

    assert setup("--config", str(config), "--clients", "", "--yes") == 0
    assert config.read_text(encoding="utf-8") == text  # nothing changed, nothing lost
    assert setup("--config", str(config), "--enable", "screen", "--clients", "", "--yes") == 0

    after = read_config_file(config)
    assert after.approval.timeout_seconds == 300
    assert after.filesystem.write is True
    assert after.screen.enabled is True
    assert (config.parent / "config.toml.bak").read_text(encoding="utf-8") == text


# Claude Desktop.


def test_desktop_gets_the_isolated_entry_and_keeps_everything_else(
    tmp_path: Path, fake: FakeClients
) -> None:
    config = tmp_path / "state" / "config.toml"
    original = fake.desktop.read_text(encoding="utf-8")

    code = setup(
        "--workspace", str(tmp_path / "scratch"), "--create-workspace", "--config", str(config),
        "--clients", "desktop", "--yes",
    )  # fmt: skip

    assert code == 0
    written = json.loads(fake.desktop.read_text(encoding="utf-8"))
    assert written["preferences"] == {"theme": "dark"}
    assert written["mcpServers"]["other"] == OTHER_SERVER
    entry = written["mcpServers"][clients.SERVER_NAME]
    assert entry["args"] == ["-I", "-m", "universal_ai_tools.server"]
    assert entry["env"] == {"UNIVERSAL_AI_TOOLS_CONFIG": str(config.resolve())}
    (backup,) = fake.desktop.parent.glob("claude_desktop_config.json.bak-*")
    assert backup.read_text(encoding="utf-8") == original
    assert fake.commands == []


# Claude Code.


def _local_env_mode_registration(project: Path) -> dict[str, Any]:
    return {
        "projects": {
            str(project): {
                "mcpServers": {
                    clients.SERVER_NAME: {
                        "type": "stdio",
                        "command": "python.exe",
                        "args": ["-m", "universal_ai_tools.server"],
                        "env": {"UNIVERSAL_AI_TOOLS_WORKSPACE": str(project)},
                    }
                }
            }
        },
        "numStartups": 3,
    }


def test_code_is_registered_for_all_projects_and_the_weaker_local_entry_removed(
    tmp_path: Path, fake: FakeClients, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    fake.code_settings.write_text(
        json.dumps(_local_env_mode_registration(project)), encoding="utf-8"
    )
    original = fake.code_settings.read_bytes()
    config = tmp_path / "state" / "config.toml"

    code = setup(
        "--workspace", str(tmp_path / "scratch"), "--create-workspace", "--config", str(config),
        "--clients", "code", "--yes",
    )  # fmt: skip

    out = capsys.readouterr().out
    assert code == 0
    assert "starts Python without -I" in out
    assert "environment mode" in out
    (backup,) = fake.code_settings.parent.glob(".claude.json.bak-*")
    assert backup.read_bytes() == original
    (add, add_cwd), (remove, remove_cwd) = fake.commands
    assert add[1:6] == ["mcp", "add-json", "--scope", "user", clients.SERVER_NAME]
    assert json.loads(add[6])["args"] == ["-I", "-m", "universal_ai_tools.server"]
    assert add_cwd is None
    assert remove[1:] == ["mcp", "remove", "--scope", "local", clients.SERVER_NAME]
    assert remove_cwd == project
    # Only MCP registrations are touched: no permission or auto-approval command.
    assert all(command[1] == "mcp" for command, _ in fake.commands)


def test_a_code_dry_run_runs_nothing_and_copies_nothing(tmp_path: Path, fake: FakeClients) -> None:
    project = tmp_path / "project"
    project.mkdir()
    fake.code_settings.write_text(
        json.dumps(_local_env_mode_registration(project)), encoding="utf-8"
    )

    code = setup(
        "--workspace", str(tmp_path / "scratch"), "--create-workspace",
        "--config", str(tmp_path / "state" / "config.toml"), "--clients", "code", "--dry-run",
    )  # fmt: skip

    assert code == 0
    assert fake.commands == []
    assert list(fake.code_settings.parent.glob(".claude.json.bak-*")) == []


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        (
            {"command": "py", "args": ["-I", "-m", "x"], "env": {"UNIVERSAL_AI_TOOLS_CONFIG": "c"}},
            None,
        ),
        (
            {"command": "py", "args": ["-P", "-m", "x"], "env": {"UNIVERSAL_AI_TOOLS_CONFIG": "c"}},
            None,
        ),
        ({"command": "py", "args": ["-m", "x"], "env": {"UNIVERSAL_AI_TOOLS_CONFIG": "c"}}, "-I"),
        (
            {
                "command": "py",
                "args": ["-I", "-m", "x"],
                "env": {"UNIVERSAL_AI_TOOLS_WORKSPACE": "w"},
            },
            "environment mode",
        ),
    ],
    ids=["isolated", "safe-path", "no-isolation", "environment-mode"],
)
def test_weaker_registrations_are_named(entry: dict[str, Any], problem: str | None) -> None:
    found = clients.registration_problem(entry)

    assert (found is None) if problem is None else (found is not None and problem in found)


# doctor.


def doctor(*arguments: str) -> int:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["doctor", *arguments])
    code = exit_info.value.code
    return code if isinstance(code, int) else 1


def test_doctor_fails_a_registration_without_isolation(
    tmp_path: Path, fake: FakeClients, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "state" / "config.toml"
    assert setup(
        "--workspace", str(tmp_path / "scratch"), "--create-workspace", "--config", str(config),
        "--clients", "desktop", "--yes",
    ) == 0  # fmt: skip
    data = json.loads(fake.desktop.read_text(encoding="utf-8"))
    data["mcpServers"][clients.SERVER_NAME]["args"] = ["-m", "universal_ai_tools.server"]
    fake.desktop.write_text(json.dumps(data), encoding="utf-8")
    capsys.readouterr()

    code = doctor("--config", str(config), "--no-handshake")

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  Claude Desktop" in out
    assert "without -I" in out
    assert "ok    isolated launch" in out
    assert "WARN  Claude Code guard: not installed" in out


def test_doctor_passes_a_complete_setup_with_a_live_handshake(
    tmp_path: Path,
    fake: FakeClients,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "state" / "config.toml"
    assert setup(
        "--workspace", str(tmp_path / "scratch"), "--create-workspace", "--config", str(config),
        "--enable", "filesystem-write", "--clients", "desktop", "--yes",
    ) == 0  # fmt: skip
    entry = clients.server_entry(config)
    monkeypatch.setattr(
        clients,
        "code_registrations",
        lambda home=None: clients.CodeRegistrations({"type": "stdio", **entry}, {}),
    )
    # A variable from this shell must not leak into the server the handshake starts.
    monkeypatch.setenv("UNIVERSAL_AI_TOOLS_WORKSPACE", str(tmp_path))
    capfd.readouterr()

    code = doctor("--config", str(config))

    out = capfd.readouterr().out
    assert code == 0, out
    assert "ok    live MCP handshake: 8 tools" in out
    assert "0 failure(s)" in out
    events = [record["event"] for record in read_records(config.parent / "audit.jsonl")]
    assert "doctor" in events
    assert "server_start" in events  # the handshake's server recorded its start


# The protected runtime and the Claude Code guard.


def fake_runtime(fake: FakeClients) -> Path:
    python = clients.runtime_python(fake.runtime)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    return python


OTHER_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}


def test_setup_installs_the_guard_and_points_claude_code_at_the_runtime(
    tmp_path: Path, fake: FakeClients
) -> None:
    python = fake_runtime(fake)
    fake.user_settings.parent.mkdir(parents=True)
    fake.user_settings.write_text(
        json.dumps({"theme": "dark", "hooks": {"PreToolUse": [OTHER_HOOK]}}), encoding="utf-8"
    )
    config = tmp_path / "state" / "config.toml"

    code = setup(
        "--workspace", str(tmp_path / "scratch"), "--create-workspace", "--config", str(config),
        "--clients", "code", "--yes",
    )  # fmt: skip

    assert code == 0
    written = json.loads(fake.user_settings.read_text(encoding="utf-8"))
    assert written["theme"] == "dark"
    expected = clients.guard_hooks(str(python), config)
    assert written["hooks"]["PreToolUse"] == [OTHER_HOOK, *expected["PreToolUse"]]
    assert written["hooks"]["PostToolUse"] == expected["PostToolUse"]
    assert written["hooks"]["PostToolUseFailure"] == expected["PostToolUseFailure"]
    pre = expected["PreToolUse"][0]["hooks"][0]
    assert pre["command"] == str(python)
    assert pre["args"] == ["-I", "-m", "universal_ai_tools.guard", "--config", str(config)]
    assert pre["onFailure"] == "block"
    assert expected["PreToolUse"][0]["matcher"] == "*"
    backups = list(fake.user_settings.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1
    (add,) = [c for c, _ in fake.commands if c[1:3] == ["mcp", "add-json"]]
    assert json.loads(add[-1])["command"] == str(python)

    before = fake.user_settings.read_bytes()
    assert setup("--config", str(config), "--clients", "code", "--yes") == 0
    assert fake.user_settings.read_bytes() == before  # already installed: untouched


def test_an_older_guard_entry_is_replaced_not_duplicated(tmp_path: Path, fake: FakeClients) -> None:
    python = fake_runtime(fake)
    config = tmp_path / "state" / "config.toml"
    old = clients.guard_hooks("C:/old/python.exe", config)
    fake.user_settings.parent.mkdir(parents=True)
    fake.user_settings.write_text(json.dumps({"hooks": old}), encoding="utf-8")

    plan = clients.plan_guard_hooks(fake.user_settings, str(python), config)

    assert plan.action == "replace"
    assert json.loads(plan.after)["hooks"] == clients.guard_hooks(str(python), config)


def test_the_guard_is_not_installed_without_a_runtime(
    tmp_path: Path, fake: FakeClients, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "state" / "config.toml"
    code = setup(
        "--workspace", str(tmp_path / "scratch"), "--create-workspace", "--config", str(config),
        "--clients", "code", "--yes",
    )  # fmt: skip
    assert code == 0
    assert not fake.user_settings.exists()
    assert "run 'universal-ai-tools install' first" in capsys.readouterr().out


def test_a_guard_dry_run_writes_nothing(tmp_path: Path, fake: FakeClients) -> None:
    fake_runtime(fake)
    config = tmp_path / "state" / "config.toml"
    assert setup(
        "--workspace", str(tmp_path / "scratch"), "--create-workspace", "--config", str(config),
        "--clients", "", "--yes",
    ) == 0  # fmt: skip
    assert setup("--config", str(config), "--clients", "code", "--dry-run") == 0
    assert not fake.user_settings.exists()


def test_guard_settings_that_are_not_json_are_never_overwritten(
    tmp_path: Path, fake: FakeClients
) -> None:
    fake.user_settings.parent.mkdir(parents=True)
    for text in ("{not json", "[]", '{"hooks": []}', '{"hooks": {"PreToolUse": {}}}'):
        fake.user_settings.write_text(text, encoding="utf-8")
        with pytest.raises(clients.ClientConfigError):
            clients.plan_guard_hooks(fake.user_settings, "python", tmp_path / "c.toml")


def test_guard_remove_takes_out_only_the_guard(tmp_path: Path, fake: FakeClients) -> None:
    config = tmp_path / "c.toml"
    hooks = clients.guard_hooks("python", config)
    hooks["PreToolUse"] = [OTHER_HOOK, *hooks["PreToolUse"]]
    fake.user_settings.parent.mkdir(parents=True)
    fake.user_settings.write_text(json.dumps({"theme": "dark", "hooks": hooks}), encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["guard", "remove", "--yes"])

    assert exit_info.value.code == 0
    assert json.loads(fake.user_settings.read_text(encoding="utf-8")) == {
        "theme": "dark",
        "hooks": {"PreToolUse": [OTHER_HOOK]},
    }
    plan = clients.plan_remove_guard_hooks(fake.user_settings)
    assert plan.action == "unchanged"


def test_a_plan_is_not_applied_over_a_file_that_changed(tmp_path: Path, fake: FakeClients) -> None:
    fake.user_settings.parent.mkdir(parents=True)
    fake.user_settings.write_text("{}", encoding="utf-8")
    plan = clients.plan_guard_hooks(fake.user_settings, "python", tmp_path / "c.toml")
    fake.user_settings.write_text('{"changed": true}', encoding="utf-8")
    with pytest.raises(clients.ClientConfigError, match="changed"):
        clients.apply_hooks(plan)
    assert json.loads(fake.user_settings.read_text(encoding="utf-8")) == {"changed": True}


def test_doctor_runs_the_installed_guard(
    tmp_path: Path,
    fake: FakeClients,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "state" / "config.toml"
    assert setup(
        "--workspace", str(tmp_path / "scratch"), "--create-workspace", "--config", str(config),
        "--clients", "", "--yes",
    ) == 0  # fmt: skip
    # This test's own Python stands in for the runtime's, so the hook really runs.
    monkeypatch.setattr(cli, "_installed_runtime", lambda: Path(sys.executable))
    plan = clients.plan_guard_hooks(fake.user_settings, sys.executable, config)
    clients.apply_hooks(plan)
    capsys.readouterr()

    doctor("--config", str(config), "--no-handshake")

    out = capsys.readouterr().out
    assert "ok    Claude Code guard: refuses a change to Claude's settings" in out, out
    assert "asks before every command" in out
    events = [r for r in read_records(config.parent / "audit.jsonl") if r["event"] == "guard_check"]
    assert events
    assert {r["session"] for r in events} == {"doctor-self-test"}


def test_doctor_fails_a_guard_that_differs_from_setup(
    tmp_path: Path,
    fake: FakeClients,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "state" / "config.toml"
    assert setup(
        "--workspace", str(tmp_path / "scratch"), "--create-workspace", "--config", str(config),
        "--clients", "", "--yes",
    ) == 0  # fmt: skip
    monkeypatch.setattr(cli, "_installed_runtime", lambda: Path(sys.executable))
    hooks = clients.guard_hooks(sys.executable, config)
    del hooks["PreToolUse"][0]["hooks"][0]["onFailure"]  # a guard that would fail open
    fake.user_settings.parent.mkdir(parents=True)
    fake.user_settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    capsys.readouterr()

    assert doctor("--config", str(config), "--no-handshake") == 1
    assert "FAIL  Claude Code guard: the PreToolUse hook" in capsys.readouterr().out

"""The Claude Code guard: what it refuses, asks about, and leaves alone, and that it fails closed.

Most tests build a ``Guard`` for a home directory under ``tmp_path`` with no runtime
locations, so what they check does not depend on this machine. ``test_the_running_code_*``
uses the real runtime set; the subprocess tests run the module as Claude Code does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from universal_ai_tools import guard, settings, trusted
from universal_ai_tools.audit import read_records
from universal_ai_tools.guard import (
    PREFIX,
    Guard,
    GuardConfigError,
    GuardSettings,
    Verdict,
    classify,
    handle,
    load_settings,
    redact,
)
from universal_ai_tools.trusted import TrustedPaths

WINDOWS = sys.platform == "win32"
SECRET = "SECRET-CONTENT-NEVER-LOGGED"  # noqa: S105 - a marker, not a password


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def config(home: Path) -> Path:
    state = home / ".universal-ai-tools"
    state.mkdir()
    path = state / "config.toml"
    path.write_text("version = 1\nworkspace = 'unused by the guard'\n", encoding="utf-8")
    return path


def make_guard(config: Path, home: Path, *, commands: str = "ask") -> Guard:
    loaded = load_settings(config)
    loaded = GuardSettings(loaded.config_path, loaded.audit_path, loaded.audit_max_bytes, commands)  # type: ignore[arg-type]
    return Guard(loaded, home=home, runtime=TrustedPaths())


@pytest.fixture
def g(config: Path, home: Path) -> Guard:
    return make_guard(config, home)


def change(g: Guard, path: Path | str, cwd: Path) -> Verdict:
    return g.check("Write", {"file_path": str(path), "content": SECRET}, str(cwd))


# The configuration.


def test_defaults(config: Path) -> None:
    loaded = load_settings(config)
    assert loaded.commands == "ask"
    assert loaded.audit_path == config.resolve().parent / "audit.jsonl"
    assert loaded.audit_max_bytes == 10 * 1024 * 1024


def test_the_light_reader_matches_the_full_schema(config: Path, tmp_path: Path) -> None:
    audit = tmp_path / "elsewhere" / "log.jsonl"
    config.write_text(
        "version = 1\nworkspace = 'w'\n"
        f"[audit]\npath = {json.dumps(str(audit))}\nmax_bytes = 70000\n"
        "[guard]\ncommands = 'claude'\n",
        encoding="utf-8",
    )
    full = settings.read_config_file(config)
    light = load_settings(config)
    assert light.commands == full.guard.commands == "claude"
    assert str(light.audit_path) == full.audit.path
    assert light.audit_max_bytes == full.audit.max_bytes
    assert guard.AUDIT_FILE_NAME == settings.AUDIT_FILE_NAME
    assert guard.MAX_CONFIG_BYTES == settings.MAX_CONFIG_BYTES
    assert "[guard]" in settings.render_config(full)
    assert settings.read_config_file(config) == full


@pytest.mark.parametrize(
    "text",
    [
        "version = 2\n",
        "workspace = 'w'\n",
        "version = 1\n[guard]\ncommands = 'never'\n",
        "version = 1\n[guard]\ncommand = 'ask'\n",
        "version = 1\n[audit]\nmax_bytes = true\n",
        "version = 1\n[audit]\nmax_bytes = 10\n",
        "version = 1\n[audit]\npath = 'relative.jsonl'\n",
        "version = 1\naudit = 3\n",
        "version = 1\n[audit\n",
    ],
)
def test_an_invalid_configuration_is_refused(config: Path, text: str) -> None:
    config.write_text(text, encoding="utf-8")
    with pytest.raises(GuardConfigError):
        load_settings(config)


def test_a_missing_or_relative_config_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GuardConfigError, match="does not exist"):
        load_settings(tmp_path / "missing.toml")
    with pytest.raises(GuardConfigError, match="absolute"):
        load_settings(Path("config.toml"))


# Changes: refused.


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/agents/x.md",
        ".claude/projects/p/transcript.jsonl",
        ".claude/projects/p/memory-not/x.md",
        ".claude.json",
        ".universal-ai-tools/config.toml",
        ".universal-ai-tools/runtime/Lib/site-packages/universal_ai_tools/guard.py",
        ".universal-ai-tools/audit.jsonl.1",
        ".bashrc",
        ".bash_profile",
        ".profile",
        ".gitconfig",
        ".config/git/config",
    ],
)
def test_changes_to_protected_home_locations_are_refused(
    g: Guard, home: Path, project: Path, relative: str
) -> None:
    verdict = change(g, home / relative, project)
    assert verdict.decision == "deny", verdict
    assert verdict.reason.startswith(PREFIX)


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/projects/C--x/memory/MEMORY.md",
        ".claude/projects/C--x/memory/topic.md",
        ".claude/plans/plan.md",
    ],
)
def test_claude_code_memory_and_plans_are_left_to_claude_code(
    g: Guard, home: Path, project: Path, relative: str
) -> None:
    assert change(g, home / relative, project).decision == "pass"


@pytest.mark.parametrize(
    "relative",
    [
        ".git/hooks/pre-commit",
        ".git/config",
        "sub/.git",
        ".hg/hgrc",
        ".svn/entries",
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/hooks/check.sh",
        "nested/.claude/settings.json",
        ".ssh/authorized_keys",
        ".aws/credentials",
    ],
)
def test_changes_that_could_run_programs_or_grant_access_are_refused(
    g: Guard, project: Path, relative: str
) -> None:
    assert change(g, project / relative, project).decision == "deny"


@pytest.mark.skipif(not WINDOWS, reason="names are case-insensitive on Windows")
def test_case_does_not_matter_on_windows(g: Guard, home: Path, project: Path) -> None:
    assert change(g, project / ".CLAUDE" / "Settings.JSON", project).decision == "deny"
    assert change(g, project / ".Git" / "HOOKS" / "x", project).decision == "deny"
    assert change(g, home / ".CLAUDE.JSON", project).decision == "deny"


def test_every_change_tool_is_checked(g: Guard, home: Path, project: Path) -> None:
    target = str(home / ".claude" / "settings.json")
    for tool, key in (
        ("Write", "file_path"),
        ("Edit", "file_path"),
        ("MultiEdit", "file_path"),
        ("NotebookEdit", "notebook_path"),
        ("FutureWriteTool", "path"),
    ):
        assert g.check(tool, {key: target}, str(project)).decision == "deny", tool


def test_a_relative_path_is_taken_from_the_working_directory(g: Guard, home: Path) -> None:
    assert change(g, ".claude.json", home).decision == "deny"
    assert change(g, "notes.txt", home).decision == "pass"


def test_the_strictest_answer_wins(g: Guard, home: Path, project: Path) -> None:
    verdict = g.check(
        "NotebookEdit",
        {"notebook_path": str(project / "ok.ipynb"), "file_path": str(home / ".claude.json")},
        str(project),
    )
    assert verdict.decision == "deny"


def test_a_hard_linked_file_is_refused(g: Guard, project: Path, tmp_path: Path) -> None:
    original = tmp_path / "elsewhere.txt"
    original.write_text("x", encoding="utf-8")
    alias = project / "alias.txt"
    try:
        os.link(original, alias)
    except OSError:
        pytest.skip("hard links are not available here")
    assert change(g, alias, project).decision == "deny"


def test_a_directory_link_to_a_protected_location_is_followed(
    g: Guard, home: Path, project: Path
) -> None:
    link = project / "settings-link"
    try:
        if WINDOWS:
            import _winapi

            _winapi.CreateJunction(str(home / ".claude"), str(link))
        else:
            link.symlink_to(home / ".claude", target_is_directory=True)
    except OSError:
        pytest.skip("cannot create a directory link here")
    assert change(g, link / "settings.json", project).decision == "deny"
    assert change(g, link / "projects" / "p" / "memory" / "m.md", project).decision == "pass"


@pytest.mark.skipif(not WINDOWS, reason="Windows path forms")
@pytest.mark.parametrize(
    "form",
    [
        "{home}\\.claude.json:stream",
        "{home}\\.claude.json::$DATA",
        "{home}\\.claude.json.",
        "{home}\\.claude.json ",
        "\\\\?\\{home}\\.claude.json",
        "\\\\localhost\\{drive}$\\{rest}\\.claude.json",
        "\\\\.\\{home}\\.claude.json",
    ],
)
def test_windows_aliases_of_a_protected_file_are_refused(
    g: Guard, home: Path, project: Path, form: str
) -> None:
    text = str(home)
    path = form.format(home=text, drive=text[0], rest=text[3:])
    assert change(g, path, project).decision == "deny", path


def test_a_nul_in_a_path_is_refused(g: Guard, project: Path) -> None:
    assert change(g, str(project) + "/a\x00b", project).decision == "deny"


def test_the_running_code_is_protected(config: Path, home: Path, project: Path) -> None:
    real = Guard(load_settings(config), home=home)
    package = Path(guard.__file__).parent
    for target in (package / "guard.py", package / "new_module.py", Path(sys.prefix) / "x.pth"):
        assert change(real, target, project).decision == "deny", target


# Changes: asked, or left to Claude Code.


@pytest.mark.parametrize(
    "relative",
    [".claude/commands/x.md", ".claude/skills/s/SKILL.md", ".mcp.json", ".vscode/tasks.json",
     ".env", "config/secrets.json", "server.pem"],
)  # fmt: skip
def test_changes_that_configure_tools_or_hold_secrets_are_asked(
    g: Guard, project: Path, relative: str
) -> None:
    verdict = change(g, project / relative, project)
    assert verdict.decision == "ask", verdict
    assert verdict.reason.startswith(PREFIX)


@pytest.mark.parametrize(
    "relative", ["main.py", "src/app/module.py", ".vscode/settings.json", "docs/.claude-notes.md"]
)
def test_ordinary_changes_are_left_to_claude_code(g: Guard, project: Path, relative: str) -> None:
    assert change(g, project / relative, project) == Verdict("pass")


# Reads.


def test_claude_codes_login_token_is_never_read(g: Guard, home: Path, project: Path) -> None:
    verdict = g.check(
        "Read", {"file_path": str(home / ".claude" / ".credentials.json")}, str(project)
    )
    assert verdict.decision == "deny"


@pytest.mark.parametrize(
    ("tool", "key", "relative"),
    [
        ("Read", "file_path", ".env"),
        ("Read", "file_path", "id_rsa"),
        ("Read", "file_path", ".ssh/config"),
        ("Grep", "path", ".aws"),
        ("Glob", "path", ".ssh"),
    ],
)
def test_reading_secrets_is_asked(
    g: Guard, project: Path, tool: str, key: str, relative: str
) -> None:
    assert g.check(tool, {key: str(project / relative)}, str(project)).decision == "ask"


def test_ordinary_reads_and_reads_of_settings_are_left_to_claude_code(
    g: Guard, home: Path, project: Path
) -> None:
    for path in (project / "main.py", home / ".claude" / "settings.json", project / ".env.example"):
        assert g.check("Read", {"file_path": str(path)}, str(project)).decision == "pass"
    assert g.check("Grep", {"pattern": "x"}, str(project)).decision == "pass"


# Commands.


def test_every_command_is_asked_by_default(g: Guard, project: Path) -> None:
    for tool in ("Bash", "PowerShell"):
        verdict = g.check(tool, {"command": "ls"}, str(project))
        assert verdict.decision == "ask"
        assert "not confined" in verdict.reason


def test_a_command_naming_a_protected_location_says_so(g: Guard, home: Path, project: Path) -> None:
    verdict = g.check("Bash", {"command": f"cat {home / '.claude.json'}"}, str(project))
    assert verdict.decision == "ask"
    assert "mentions" in verdict.reason


def test_with_commands_left_to_claude_only_risky_ones_are_asked(
    config: Path, home: Path, project: Path
) -> None:
    lenient = make_guard(config, home, commands="claude")
    assert lenient.check("Bash", {"command": "pytest -q"}, str(project)) == Verdict("pass")
    for command in (
        "rm ~/.claude/settings.json",
        "echo '{\"disableAllHooks\": true}' > .claude/settings.json",
        "git config core.hooksPath x",
        "Add-Content $PROFILE 'x'",
        "schtasks /create /tn x /tr y",
    ):
        assert lenient.check("Bash", {"command": command}, str(project)).decision == "ask", command


def test_a_command_that_is_not_text_is_refused(g: Guard, project: Path) -> None:
    assert g.check("Bash", {}, str(project)).decision == "deny"
    assert g.check("PowerShell", {"command": ["ls"]}, str(project)).decision == "deny"


def test_classification() -> None:
    assert classify("Write") == classify("Edit") == classify("FooEditor") == "change"
    assert classify("Read") == classify("Glob") == classify("Grep") == "read"
    assert classify("Bash") == classify("PowerShell") == "command"
    assert classify("mcp__other__write_file") == classify("WebFetch") == "other"
    assert classify("Agent") == "other"


def test_other_tools_are_left_to_claude_code(g: Guard, home: Path, project: Path) -> None:
    for tool in ("mcp__universal-ai-tools__write_file", "WebFetch", "Agent"):
        target = {"path": str(home / ".claude.json")}
        assert g.check(tool, target, str(project)) == Verdict("pass")


# Redaction.


@pytest.mark.parametrize(
    ("command", "hidden"),
    [
        ("export API_KEY=abc123def", "abc123def"),
        ("mysql --password=hunter22 db", "hunter22"),
        ("tool --token s3cr3tvalue", "s3cr3tvalue"),
        ("curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x'", "eyJhbGciOiJIUzI1NiJ9"),
        ("git clone https://user:pa55word@example.com/r.git", "pa55word"),
        ('$env:GITHUB_TOKEN = "ghp_abcdefgh"', "ghp_abcdefgh"),
    ],
)
def test_credential_looking_values_are_redacted(command: str, hidden: str) -> None:
    redacted = redact(command)
    assert hidden not in redacted
    assert "<redacted>" in redacted


def test_redaction_keeps_ordinary_commands_and_bounds_length() -> None:
    assert redact("pytest -q tests/test_guard.py") == "pytest -q tests/test_guard.py"
    assert len(redact("x" * 10_000)) == guard.COMMAND_RECORD_CHARS


# The hook protocol, in process.


def event(
    name: str, tool: str, tool_input: Mapping[str, object], cwd: Path, **extra: object
) -> bytes:
    return json.dumps(
        {
            "hook_event_name": name,
            "tool_name": tool,
            "tool_input": tool_input,
            "cwd": str(cwd),
            "session_id": "s1",
            "tool_use_id": "u1",
            "permission_mode": "auto",
            **extra,
        }
    ).encode()


@pytest.fixture
def fake_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_a_refusal_exits_2_with_the_reason(config: Path, fake_home: Path, project: Path) -> None:
    raw = event("PreToolUse", "Edit", {"file_path": str(fake_home / ".claude.json")}, project)
    code, out, err = handle(raw, config)
    assert (code, out) == (2, "")
    assert err.startswith(PREFIX)
    assert ".claude.json" in err


def test_an_ask_is_the_documented_json(config: Path, fake_home: Path, project: Path) -> None:
    code, out, err = handle(event("PreToolUse", "Bash", {"command": "ls"}, project), config)
    assert (code, err) == (0, "")
    output = json.loads(out)
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": output["hookSpecificOutput"]["permissionDecisionReason"],
        }
    }
    assert output["hookSpecificOutput"]["permissionDecisionReason"].startswith(PREFIX)


def test_the_guard_never_allows(config: Path, fake_home: Path, project: Path) -> None:
    code, out, _ = handle(
        event("PreToolUse", "Write", {"file_path": str(project / "a")}, project), config
    )
    assert (code, out) == (0, "")


def test_every_call_is_recorded_without_contents(
    config: Path, fake_home: Path, project: Path
) -> None:
    target = str(project / "a.txt")
    handle(event("PreToolUse", "Write", {"file_path": target, "content": SECRET}, project), config)
    handle(event("PostToolUse", "Write", {"file_path": target, "content": SECRET}, project,
                 tool_response={"content": SECRET}, duration_ms=5), config)  # fmt: skip
    handle(event("PreToolUse", "Edit", {"file_path": target, "old_string": SECRET,
                 "new_string": SECRET}, project), config)  # fmt: skip
    handle(event("PostToolUseFailure", "Edit", {"file_path": target}, project,
                 error="String not found\nmore", is_interrupt=False), config)  # fmt: skip
    handle(event("PostToolUseFailure", "Bash", {"command": "sleep 9"}, project,
                 error="", is_interrupt=True), config)  # fmt: skip
    handle(event("PostToolUse", "Read", {"file_path": target}, project), config)

    log = config.parent / "audit.jsonl"
    assert SECRET not in log.read_text(encoding="utf-8")
    records = read_records(log)
    summary = [(r["event"], r["tool"], r.get("decision"), r.get("status")) for r in records]
    assert summary == [
        ("guard_check", "Write", "pass", None),
        ("guard_finished", "Write", None, "succeeded"),
        ("guard_check", "Edit", "pass", None),
        ("guard_finished", "Edit", None, "failed"),
        ("guard_finished", "Bash", None, "interrupted"),
        ("guard_finished", "Read", None, "succeeded"),
    ]
    first = records[0]
    assert first["targets"] == [target]
    assert first["session"] == "s1"
    assert first["tool_use_id"] == "u1"
    assert first["permission_mode"] == "auto"
    assert first["source"] == "claude_code_guard"
    assert len(str(first["input_sha256"])) == 64
    assert records[1]["duration_ms"] == 5
    assert records[3]["error"] == "String not found"


def test_a_command_is_recorded_redacted(config: Path, fake_home: Path, project: Path) -> None:
    handle(event("PreToolUse", "Bash", {"command": "deploy --token abcdef123456"}, project), config)
    (record,) = read_records(config.parent / "audit.jsonl")
    assert record["decision"] == "ask"
    assert record["command"] == "deploy --token <redacted>"
    assert "abcdef123456" not in json.dumps(record)


def test_refusals_of_reads_are_recorded_but_plain_reads_only_afterwards(
    config: Path, fake_home: Path, project: Path
) -> None:
    handle(event("PreToolUse", "Read", {"file_path": str(project / "a.py")}, project), config)
    handle(event("PreToolUse", "Read", {"file_path": str(project / ".env")}, project), config)
    records = read_records(config.parent / "audit.jsonl")
    assert [(r["event"], r["decision"]) for r in records] == [("guard_check", "ask")]


def _break_audit(config: Path) -> None:
    (config.parent / "audit.jsonl").mkdir()


def test_a_change_or_command_that_cannot_be_recorded_is_refused(
    config: Path, fake_home: Path, project: Path
) -> None:
    _break_audit(config)
    for tool, tool_input in (
        ("Write", {"file_path": str(project / "a.txt")}),
        ("Bash", {"command": "ls"}),
    ):
        code, out, err = handle(event("PreToolUse", tool, tool_input, project), config)
        assert (code, out) == (2, ""), tool
        assert "audit" in err


def test_an_unrecordable_read_still_runs(config: Path, fake_home: Path, project: Path) -> None:
    _break_audit(config)
    assert (
        handle(event("PreToolUse", "Read", {"file_path": str(project / "a")}, project), config)[0]
        == 0
    )
    code, _, err = handle(
        event("PostToolUse", "Read", {"file_path": str(project / "a")}, project), config
    )
    assert code == 1
    assert "audit" in err


def test_without_a_configuration_only_reads_run(
    tmp_path: Path, fake_home: Path, project: Path
) -> None:
    missing = tmp_path / "no-config.toml"
    for tool, tool_input in (
        ("Write", {"file_path": str(project / "a.txt")}),
        ("Bash", {"command": "ls"}),
        ("WebFetch", {"url": "https://example.com"}),
    ):
        code, _, err = handle(event("PreToolUse", tool, tool_input, project), missing)
        assert code == 2, tool
        assert "doctor" in err
    read = {"file_path": str(project / "a.py")}
    assert handle(event("PreToolUse", "Read", read, project), missing) == (0, "", "")
    token = {"file_path": str(fake_home / ".claude" / ".credentials.json")}
    assert handle(event("PreToolUse", "Read", token, project), missing)[0] == 2


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[1, 2]", b'{"hook_event_name": "PreToolUse"}',
     b'{"hook_event_name": "PreToolUse", "tool_name": ""}'],
)  # fmt: skip
def test_a_malformed_event_is_refused(config: Path, raw: bytes) -> None:
    assert handle(raw, config)[0] == 2


def test_other_events_are_ignored(config: Path) -> None:
    assert handle(b'{"hook_event_name": "SessionStart"}', config) == (0, "", "")


def test_an_unexpected_error_fails_closed(
    config: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    def explode(raw: bytes, path: Path) -> tuple[int, str, str]:
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "handle", explode)
    monkeypatch.setattr(sys, "stdin", _Stdin(b"{}"))
    assert guard.main(["--config", str(config)]) == 2
    assert "failed unexpectedly" in capfd.readouterr().err


class _Stdin:
    def __init__(self, data: bytes) -> None:
        import io

        self.buffer = io.BytesIO(data)


# As Claude Code runs it.


def run_hook(
    config: Path, payload: bytes, *, cwd: Path, flags: tuple[str, ...] = ("-I",)
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, *flags, "-m", "universal_ai_tools.guard", "--config", str(config)],
        input=payload,
        capture_output=True,
        cwd=cwd,
        timeout=60,
        check=False,
    )


def test_the_module_refuses_and_asks_as_a_process(config: Path, home: Path, project: Path) -> None:
    refused = run_hook(config, event("PreToolUse", "Write",
                       {"file_path": str(Path(guard.__file__))}, project), cwd=project)  # fmt: skip
    assert refused.returncode == 2
    assert refused.stdout == b""
    assert PREFIX.encode() in refused.stderr
    asked = run_hook(
        config, event("PreToolUse", "PowerShell", {"command": "dir"}, project), cwd=project
    )
    assert asked.returncode == 0
    assert json.loads(asked.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_a_project_cannot_replace_the_guards_code(config: Path, project: Path) -> None:
    fake = project / "universal_ai_tools"
    fake.mkdir()
    (fake / "__init__.py").write_text("", encoding="utf-8")
    (fake / "guard.py").write_text("print('HIJACKED'); raise SystemExit(0)", encoding="utf-8")
    result = run_hook(config, event("PreToolUse", "Bash", {"command": "ls"}, project), cwd=project)
    assert b"HIJACKED" not in result.stdout
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_trusted_exports_used_by_the_guard() -> None:
    assert trusted.is_within(trusted.normalize("/a/b"), trusted.normalize("/a"))
    assert not trusted.is_within(trusted.normalize("/ab"), trusted.normalize("/a"))
    assert callable(trusted.real_path)

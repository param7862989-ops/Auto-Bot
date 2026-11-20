"""The Claude Code guard: a hook that checks Claude Code's own tools before they run.

The MCP server checks every call that comes to it, but Claude Code's built-in tools (Bash,
PowerShell, Write, Edit, Read, ...) never reach the server. Claude Code runs this module as a
``PreToolUse``, ``PostToolUse``, and ``PostToolUseFailure`` hook for every tool (``setup``
registers it in ``~/.claude/settings.json``), so those tools are checked by code that runs
outside the model, and recorded in the same audit log as the server's tools.

Before a tool runs (``PreToolUse``) the guard answers one of three ways:

* **deny**: the call does not run, whatever any permission rule, mode, or approval says.
  For changes (Write, Edit, MultiEdit, NotebookEdit) to:

  - what the server protects (``universal_ai_tools.trusted``): Universal AI Tools' own
    code and the Python running it, its config file, state directory, and audit log,
    Windows' Startup and PowerShell profile folders, and Claude's own settings
    (``~/.claude``, ``~/.claude.json``, Claude Desktop's folder), except Claude Code's
    memory and plan files (``~/.claude/projects/*/memory``, ``~/.claude/plans``);
  - a project's Claude settings (``.claude/settings.json``, ``.claude/settings.local.json``,
    ``.claude/hooks``): a project's settings can switch this guard off
    (``disableAllHooks``) or add hooks that run programs;
  - repository metadata (``.git``, ``.hg``, ``.svn``): Git hooks and ``core.hooksPath``
    or ``core.fsmonitor`` run programs on the next Git command;
  - shell startup files in your home directory (``.bashrc``, ``.profile``, ...) and Git's
    user configuration (``.gitconfig``, ``.config/git``): Claude Code's Bash tool runs
    Git Bash, which reads them;
  - credential directories (``.ssh``, ``.aws``, ...): ``authorized_keys`` grants a login;
  - an existing file with more than one hard link (it may be a protected file under
    another name), a network or device path, or an alternate data stream.

  For reads: Claude Code's own login token (``~/.claude/.credentials.json``).
* **ask**: Claude Code shows its permission prompt with the guard's reason, even if an allow
  rule matches or auto mode would otherwise decide. Every Bash and PowerShell command (with
  ``[guard] commands = "ask"``, the default: a started program runs with your rights and
  nothing here can confine it), and a command that names a protected location even with
  ``commands = "claude"``. Changes to other files in a project's ``.claude`` folder, to
  ``.mcp.json`` and ``.vscode/tasks.json`` (both can start programs), and to credential-
  looking files; reads of credential-looking files and directories.
* **no answer**: Claude Code's own permission rules and mode decide, as without the guard.

The guard never answers "allow", so it only ever adds checks.

It fails closed. If it cannot read its configuration, it refuses every tool except reads
(which still get the name checks above); if anything else goes wrong, or the audit record of
a change or command cannot be written, the call is refused (exit code 2). ``setup`` also
registers it with ``"onFailure": "block"``, so a guard that cannot start blocks the call
instead of letting it through.

Every tool call is recorded: changes and commands before they run (with the decision) and
after; reads and other tools after. Records hold the tool, the paths, the decision, and a
command line with credential-looking values redacted (``redact``); never file contents or
edit text.

What the guard does not do. It does not confine commands: an approved command can change
anything you can, including everything listed above, so read what you approve. Its check of
command text for protected names is a warning for you, not a barrier, and is easy to evade.
It checks paths as Claude Code passes them; it does not see what an MCP server other than
Universal AI Tools does (that server has its own approvals). A project's settings could still
switch it off if changed by a command or by hand; a managed settings file with
``allowManagedHooksOnly`` (which needs an administrator) is the only setting Claude Code
offers that a project cannot override.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from universal_ai_tools import __version__
from universal_ai_tools.audit import AuditError, AuditLog
from universal_ai_tools.policy import (
    METADATA_NAMES,
    SENSITIVE_DIRECTORY_NAMES,
    is_sensitive_file_name,
)
from universal_ai_tools.trusted import (
    TrustedPaths,
    is_within,
    normalize,
    real_path,
)

# settings.AUDIT_FILE_NAME and settings.MAX_CONFIG_BYTES; not imported, because settings
# needs pydantic and the guard runs twice for every tool call. A test checks they agree.
AUDIT_FILE_NAME = "audit.jsonl"
MAX_CONFIG_BYTES = 256 * 1024
MAX_EVENT_BYTES = 64 * 1024 * 1024
MAX_TARGETS = 10
COMMAND_RECORD_CHARS = 2000

SHELL_STARTUP = "a file your shell or Git reads and runs commands from"
PREFIX = "Universal AI Tools guard: "

Decision = Literal["pass", "ask", "deny"]
Kind = Literal["change", "read", "command", "other"]
CommandPolicy = Literal["ask", "claude"]

CHANGE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
READ_TOOLS = frozenset({"Read", "Glob", "Grep", "NotebookRead", "LS"})
COMMAND_TOOLS = frozenset({"Bash", "PowerShell"})
PATH_KEYS = ("file_path", "notebook_path", "path")

# In the home directory: Git Bash (Claude Code's Bash tool on Windows) reads these at start.
HOME_SHELL_FILES = (
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".bash_logout",
    ".profile",
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".inputrc",
    ".gitconfig",
    ".config/git",
)
CLAUDE_PROJECT_DENIED = frozenset({"settings.json", "settings.local.json", "hooks"})
ASK_FILES = {
    ".mcp.json": "it can add MCP servers, which are programs Claude Code starts",
}
ASK_NESTED = {
    (".vscode", "tasks.json"): "VS Code can run its tasks when the folder is opened",
}
# Words that, in a command, suggest it touches something the guard protects. A hint for the
# person approving the command, not a barrier.
COMMAND_MARKERS = (
    ".claude",
    ".universal-ai-tools",
    "universal_ai_tools",
    ".mcp.json",
    "disableallhooks",
    "managed-settings",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".gitconfig",
    "hookspath",
    "fsmonitor",
    ".git/hooks",
    ".git\\hooks",
    "$profile",
    "start menu\\programs\\startup",
    "start menu/programs/startup",
    "currentversion\\run",
    "schtasks",
    "register-scheduledtask",
    "input resume",
    "input-stopped",
    "grants-revoked",
)


class GuardConfigError(Exception):
    """The guard's configuration is missing or invalid."""


@dataclass(frozen=True)
class GuardSettings:
    config_path: Path
    audit_path: Path
    audit_max_bytes: int = 10 * 1024 * 1024
    commands: CommandPolicy = "ask"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str = ""


PASS = Verdict("pass")


def load_settings(config_path: Path) -> GuardSettings:
    """Read what the guard needs from the config file, without pydantic.

    The server validates the whole file (``settings.ConfigFile``); this reads only the
    ``version``, ``[audit]``, and ``[guard]`` keys, as strictly.

    Raises:
        GuardConfigError: If the file is missing, unreadable, or those keys are invalid.
    """
    if not config_path.is_absolute():
        raise GuardConfigError(f"the config path must be absolute, got '{config_path}'")
    try:
        resolved = config_path.resolve(strict=True)
        if not resolved.is_file():
            raise GuardConfigError(f"'{config_path}' is not a file")
        if resolved.stat().st_size > MAX_CONFIG_BYTES:
            raise GuardConfigError(f"'{config_path}' is larger than {MAX_CONFIG_BYTES} bytes")
        data = tomllib.loads(resolved.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise GuardConfigError(f"the config file '{config_path}' does not exist") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GuardConfigError(f"cannot read '{config_path}': {type(exc).__name__}") from None
    if data.get("version") != 1:
        raise GuardConfigError(f"'{config_path}' has no 'version = 1'")
    audit = _table(data, "audit", {"path", "max_bytes"})
    path = audit.get("path", "")
    max_bytes = audit.get("max_bytes", 10 * 1024 * 1024)
    if not isinstance(path, str):
        raise GuardConfigError("audit.path must be a string")
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 64 * 1024 <= max_bytes <= 1024 * 1024 * 1024
    ):
        raise GuardConfigError("audit.max_bytes must be a whole number from 65536 to 1073741824")
    audit_path = Path(path) if path else resolved.parent / AUDIT_FILE_NAME
    if not audit_path.is_absolute():
        raise GuardConfigError("audit.path must be absolute")
    guard = _table(data, "guard", {"commands"})
    commands = guard.get("commands", "ask")
    if commands not in ("ask", "claude"):
        raise GuardConfigError('guard.commands must be "ask" or "claude"')
    return GuardSettings(resolved, audit_path, max_bytes, commands)


def _table(data: Mapping[str, object], name: str, keys: set[str]) -> Mapping[str, object]:
    table = data.get(name, {})
    if not isinstance(table, dict):
        raise GuardConfigError(f"[{name}] must be a table")
    unknown = sorted(set(table) - keys)
    if unknown:
        raise GuardConfigError(f"unknown key(s) in [{name}]: {', '.join(unknown)}")
    return table


class Guard:
    """The decisions, for one configuration. ``check`` is pure apart from reading paths."""

    def __init__(
        self,
        settings: GuardSettings,
        *,
        home: Path | None = None,
        runtime: TrustedPaths | None = None,
    ) -> None:
        self.settings = settings
        home = home or Path.home()
        self._home = real_path(home)
        self._claude = real_path(home / ".claude")
        self._credentials = real_path(home / ".claude" / ".credentials.json")
        self.trusted = TrustedPaths.for_state(
            settings.config_path, settings.audit_path, runtime=runtime, home=home
        ).combined(TrustedPaths((home / name, SHELL_STARTUP) for name in HOME_SHELL_FILES))
        self.audit = AuditLog(settings.audit_path, max_bytes=settings.audit_max_bytes)

    # PreToolUse

    def check(self, tool: str, tool_input: Mapping[str, object], cwd: str) -> Verdict:
        kind = classify(tool)
        if kind == "change":
            return strictest(self.check_change(p, cwd) for p in targets(tool_input))
        if kind == "read":
            return strictest(check_read(p, cwd, self._credentials) for p in targets(tool_input))
        if kind == "command":
            return self.check_command(tool_input.get("command"))
        return PASS

    def check_change(self, raw: str, cwd: str) -> Verdict:
        resolved = _resolve(raw, cwd)
        if isinstance(resolved, Verdict):
            return resolved
        path, parts = resolved
        if is_within(path, self._claude) and _claude_data(path, self._claude):
            if _is_hard_linked(path):
                return _deny(f"'{raw}' has more than one hard link.")
            return PASS
        entry = self.trusted.protecting(path, normalize(cwd))
        if entry is not None:
            return _deny(f"'{raw}' is protected ({entry.reason}); Claude may not change it.")
        for index, part in enumerate(parts):
            if part in METADATA_NAMES:
                return _deny(
                    f"'{raw}' is repository metadata ({part}); hooks and settings there run "
                    "programs. Use the version-control tool itself."
                )
            if part in SENSITIVE_DIRECTORY_NAMES:
                return _deny(f"'{raw}' is in a credential directory ({part}).")
            if (
                part == ".claude"
                and index + 1 < len(parts)
                and parts[index + 1] in CLAUDE_PROJECT_DENIED
            ):
                return _deny(
                    f"'{raw}' is a project's Claude settings, which can switch this guard "
                    "off or add hooks that run programs. Edit it yourself if you mean to."
                )
        if _is_hard_linked(path):
            return _deny(
                f"'{raw}' has more than one hard link, so changing it changes another file too."
            )
        if ".claude" in parts[:-1]:
            return _ask(f"'{raw}' is in a project's .claude folder, which configures Claude Code.")
        name = parts[-1] if parts else ""
        if name in ASK_FILES:
            return _ask(f"'{raw}': {ASK_FILES[name]}.")
        for (folder, file), why in ASK_NESTED.items():
            if len(parts) >= 2 and parts[-2] == folder and name == file:
                return _ask(f"'{raw}': {why}.")
        if name and is_sensitive_file_name(name):
            return _ask(f"'{raw}' looks like a credential or secrets file.")
        return PASS

    def check_command(self, command: object) -> Verdict:
        if not isinstance(command, str):
            return _deny("the command is missing or not text.")
        folded = command.casefold()
        named = [marker for marker in COMMAND_MARKERS if marker in folded]
        named += [
            entry.reason
            for entry in self.trusted
            if not entry.above_exempt and len(entry.path) > 3 and entry.path in folded
        ]
        mention = (
            f" It mentions {', '.join(sorted(set(named))[:4])}, which the guard protects; "
            "approve only if you expect that."
            if named
            else ""
        )
        if self.settings.commands == "ask":
            return _ask(
                "asks before every command: an approved program runs with your rights and is "
                "not confined." + mention
            )
        if named:
            return _ask("this command may touch protected settings or code." + mention)
        return PASS


def strictest(verdicts: Iterable[Verdict]) -> Verdict:
    order = {"pass": 0, "ask": 1, "deny": 2}
    best = PASS
    for verdict in verdicts:
        if order[verdict.decision] > order[best.decision]:
            best = verdict
    return best


def classify(tool: str) -> Kind:
    if tool in CHANGE_TOOLS:
        return "change"
    if tool in READ_TOOLS:
        return "read"
    if tool in COMMAND_TOOLS:
        return "command"
    if not tool.startswith("mcp__") and ("Edit" in tool or "Write" in tool):
        # A built-in file tool this version does not know: treated as a change.
        return "change"
    return "other"


def targets(tool_input: Mapping[str, object]) -> list[str]:
    found = [tool_input[key] for key in PATH_KEYS if key in tool_input]
    return [value for value in found if isinstance(value, str) and value]


def check_read(raw: str, cwd: str, credentials: str) -> Verdict:
    resolved = _resolve(raw, cwd, reading=True)
    if isinstance(resolved, Verdict):
        return PASS
    path, parts = resolved
    if path == credentials:
        return _deny("this is Claude Code's own login token.")
    if any(part in SENSITIVE_DIRECTORY_NAMES for part in parts):
        return _ask(f"'{raw}' is in a credential directory.")
    if parts and is_sensitive_file_name(parts[-1]):
        return _ask(f"'{raw}' looks like a credential or secrets file.")
    return PASS


def _resolve(raw: str, cwd: str, *, reading: bool = False) -> tuple[str, list[str]] | Verdict:
    """The real, normalized path and its lower-cased components, or a refusal."""
    if "\x00" in raw:
        return _deny("the path contains a NUL character.")
    path = raw if os.path.isabs(raw) else os.path.join(cwd or os.getcwd(), raw)
    if not os.path.isabs(path):
        return _deny(f"'{raw}' is not an absolute path.")
    try:
        real = real_path(path)
    except (OSError, ValueError):
        return _deny(f"'{raw}' cannot be resolved.")
    if sys.platform == "win32" and not reading:
        if real.startswith("\\\\"):
            return _deny(f"'{raw}' is a network or device path.")
        if ":" in real[2:]:
            return _deny(f"'{raw}' names an alternate data stream.")
    parts = [part.casefold() for part in Path(real).parts[1:]]
    if sys.platform == "win32":
        if not reading and any(part != part.rstrip(". ") for part in parts):
            return _deny(f"'{raw}' has a name ending in a dot or space, which Windows ignores.")
    return real, parts


def _claude_data(path: str, claude: str) -> bool:
    """Claude Code's memory and plans: data it writes with its own file tools."""
    relative = Path(path[len(claude) :].lstrip("\\/")).parts
    lowered = [part.casefold() for part in relative]
    if len(lowered) >= 3 and lowered[0] == "projects" and lowered[2] == "memory":
        return True
    return len(lowered) >= 1 and lowered[0] == "plans"


def _is_hard_linked(path: str) -> bool:
    try:
        st = os.stat(path, follow_symlinks=True)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink > 1


def _deny(reason: str) -> Verdict:
    return Verdict("deny", PREFIX + reason)


def _ask(reason: str) -> Verdict:
    return Verdict("ask", PREFIX + reason)


# Audit records.

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:[a-z0-9_]*(?:password|passwd|pwd|token|secret|api[-_]?key|access[-_]?key|"
    r"private[-_]?key|credential)[a-z0-9_]*)\b\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"
)
_SECRET_FLAG = re.compile(
    r"(?i)(--?(?:password|passwd|token|secret|api[-_]?key|access[-_]?key)(?:\s+|=))"
    r"(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"
)
_BEARER = re.compile(r"(?i)(\b(?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]{8,})")
_URL_CREDENTIALS = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)")
REDACTED = "<redacted>"


def redact(command: str) -> str:
    """``command`` with credential-looking values replaced. Best effort, for the log only."""
    text = _SECRET_ASSIGNMENT.sub(lambda m: m.group(1) + REDACTED, command)
    text = _SECRET_FLAG.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _URL_CREDENTIALS.sub(lambda m: m.group(1) + REDACTED + m.group(3), text)
    if len(text) > COMMAND_RECORD_CHARS:
        text = text[: COMMAND_RECORD_CHARS - 1] + "…"
    return text


def _fields(event: Mapping[str, object], kind: Kind) -> dict[str, object]:
    tool_input = event.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    canonical = json.dumps(tool_input, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    fields: dict[str, object] = {
        "source": "claude_code_guard",
        "version": __version__,
        "session": event.get("session_id"),
        "tool_use_id": event.get("tool_use_id"),
        "tool": event.get("tool_name"),
        "kind": kind,
        "cwd": event.get("cwd"),
        "permission_mode": event.get("permission_mode"),
        "input_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }
    for key in ("agent_id", "agent_type"):
        if event.get(key):
            fields[key] = event.get(key)
    found = targets(tool_input)
    if found:
        fields["targets"] = found[:MAX_TARGETS]
    command = tool_input.get("command")
    if kind == "command" and isinstance(command, str):
        fields["command"] = redact(command)
        fields["command_chars"] = len(command)
    return fields


# The hook entry point.


def pre_tool_use(event: Mapping[str, object], config_path: Path) -> tuple[int, str, str]:
    """``(exit code, stdout, stderr)`` for a ``PreToolUse`` event."""
    tool = event.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return 2, "", PREFIX + "the hook event names no tool; refusing."
    tool_input = event.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    cwd = event.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    kind = classify(tool)
    try:
        guard = Guard(load_settings(config_path))
    except GuardConfigError as exc:
        if kind == "read":
            home = Path.home()
            credentials = real_path(home / ".claude" / ".credentials.json")
            verdict = strictest(check_read(p, cwd, credentials) for p in targets(tool_input))
            return _answer(verdict)
        return (
            2,
            "",
            (
                PREFIX + f"cannot load its configuration ({exc}), so only reads are allowed. "
                "Run 'universal-ai-tools doctor' in your own terminal."
            ),
        )
    verdict = guard.check(tool, tool_input, cwd)
    if kind in ("change", "command") or verdict.decision != "pass":
        fields = {**_fields(event, kind), "decision": verdict.decision}
        if verdict.reason:
            fields["reason"] = verdict.reason.removeprefix(PREFIX)
        try:
            guard.audit.record("guard_check", fields, durable=kind in ("change", "command"))
        except AuditError as exc:
            if kind in ("change", "command") and verdict.decision != "deny":
                return (
                    2,
                    "",
                    PREFIX + f"{exc}; a change or command that cannot be recorded is refused.",
                )
    return _answer(verdict)


def _answer(verdict: Verdict) -> tuple[int, str, str]:
    if verdict.decision == "deny":
        return 2, "", verdict.reason
    if verdict.decision == "ask":
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": verdict.reason,
            }
        }
        return 0, json.dumps(output), ""
    return 0, "", ""


def post_tool_use(event: Mapping[str, object], config_path: Path) -> tuple[int, str, str]:
    """Record what happened. Never blocks: the tool has already run."""
    tool = event.get("tool_name")
    kind = classify(tool) if isinstance(tool, str) else "other"
    try:
        settings = load_settings(config_path)
    except GuardConfigError as exc:
        return 1, "", PREFIX + f"not recorded: {exc}"
    fields = _fields(event, kind)
    if event.get("hook_event_name") == "PostToolUseFailure":
        fields["status"] = "interrupted" if event.get("is_interrupt") else "failed"
        error = event.get("error")
        if isinstance(error, str):
            fields["error"] = error.splitlines()[0][:200] if error else ""
    else:
        fields["status"] = "succeeded"
    duration = event.get("duration_ms")
    if isinstance(duration, int | float) and not isinstance(duration, bool):
        fields["duration_ms"] = duration
    audit = AuditLog(settings.audit_path, max_bytes=settings.audit_max_bytes)
    if not audit.try_record("guard_finished", fields):
        return 1, "", PREFIX + "could not write the audit record."
    return 0, "", ""


def handle(raw: bytes, config_path: Path) -> tuple[int, str, str]:
    """One hook call: the event's JSON in, ``(exit code, stdout, stderr)`` out."""
    try:
        event = json.loads(raw)
    except ValueError:
        return 2, "", PREFIX + "the hook event is not valid JSON; refusing."
    if not isinstance(event, dict):
        return 2, "", PREFIX + "the hook event is not a JSON object; refusing."
    name = event.get("hook_event_name")
    if name == "PreToolUse":
        return pre_tool_use(event, config_path)
    if name in ("PostToolUse", "PostToolUseFailure"):
        return post_tool_use(event, config_path)
    return 0, "", ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m universal_ai_tools.guard")
    parser.add_argument("--config", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
        if len(raw) > MAX_EVENT_BYTES:
            code, out, err = 2, "", PREFIX + "the hook event is too large; refusing."
        else:
            code, out, err = handle(raw, options.config)
    except Exception as exc:  # fail closed on anything unexpected
        code, out, err = 2, "", PREFIX + f"failed unexpectedly ({type(exc).__name__}); refusing."
    if out:
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.buffer.flush()
    if err:
        sys.stderr.buffer.write(err.encode("utf-8") + b"\n")
        sys.stderr.buffer.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())

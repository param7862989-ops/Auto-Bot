"""Registering the server with Claude Desktop and Claude Code, without losing their settings.

Both registrations launch the same Python and module with one environment variable,
``UNIVERSAL_AI_TOOLS_CONFIG``, so both clients share one configuration (see
``settings.py``).

* **Claude Desktop** reads ``claude_desktop_config.json``. With the Microsoft Store (MSIX)
  build that file lives in the app's package folder,
  ``%LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\Roaming\\Claude``; with the classic
  installer, in ``%APPDATA%\\Claude``. A change is planned as a full before-and-after text,
  every other key is kept exactly, the old file is copied to ``*.bak-<time>`` first, and the
  new file is written atomically. Invalid JSON is never overwritten.
* **Claude Code** is changed only through its own CLI (``claude mcp add-json --scope user``).
  Its settings file (``~/.claude.json``) is read, to find existing registrations, including
  per-project (``local``) ones that would shadow the user-scope entry, and copied to
  ``~/.claude.json.bak-<time>`` before any ``claude mcp`` command runs. Nothing here touches
  Claude Code's permission settings: no tool is ever auto-approved.

This module runs ``claude`` as a child process, so it is, with ``terminal.py``, one of the two
modules allowed to start processes; the MCP server never imports it.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from universal_ai_tools.settings import ENV_CONFIG

SERVER_NAME = "universal-ai-tools"
DESKTOP_CONFIG_NAME = "claude_desktop_config.json"


def server_entry(config_path: Path, python: str | None = None) -> dict[str, Any]:
    """The MCP server registration both clients get.

    ``-I`` (isolated mode): Python does not put the current directory on ``sys.path`` and
    ignores ``PYTHON*`` variables and the per-user site directory. A client may start the
    server in a project directory, and without ``-I`` a file such as ``json.py`` or a
    ``universal_ai_tools`` folder there would be imported in place of the server's own code.
    """
    return {
        "command": python or sys.executable,
        "args": ["-I", "-m", "universal_ai_tools.server"],
        "env": {ENV_CONFIG: str(config_path)},
    }


# Claude Desktop


def desktop_config_candidates(environ: dict[str, str] | None = None) -> list[Path]:
    environ = dict(os.environ) if environ is None else environ
    found: list[Path] = []
    local = environ.get("LOCALAPPDATA")
    if local:
        packages = Path(local) / "Packages"
        try:
            for package in sorted(packages.glob("Claude_*")):
                found.append(package / "LocalCache" / "Roaming" / "Claude" / DESKTOP_CONFIG_NAME)
        except OSError:
            pass
    appdata = environ.get("APPDATA")
    if appdata:
        found.append(Path(appdata) / "Claude" / DESKTOP_CONFIG_NAME)
    if sys.platform == "darwin":
        found.append(
            Path.home() / "Library" / "Application Support" / "Claude" / DESKTOP_CONFIG_NAME
        )
    return found


def desktop_config_path(environ: dict[str, str] | None = None) -> Path | None:
    """The file Claude Desktop reads: the Store build's if it is installed, else the classic one.

    None if Claude Desktop does not seem to be installed (no settings folder).
    """
    candidates = desktop_config_candidates(environ)
    for candidate in candidates:
        if candidate.parent.is_dir():
            return candidate
    for candidate in candidates:
        # MSIX package installed but never started: the folder will exist after first launch.
        if candidate.parent.parent.parent.is_dir() and "Packages" in candidate.parts:
            return candidate
    return None


@dataclass(frozen=True)
class DesktopPlan:
    path: Path
    action: Literal["add", "replace", "unchanged"]
    before: str
    after: str
    previous_entry: dict[str, Any] | None

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.before.splitlines(keepends=True),
                self.after.splitlines(keepends=True),
                fromfile=f"{self.path} (now)",
                tofile=f"{self.path} (after)",
            )
        )


class ClientConfigError(Exception):
    """A client's configuration cannot be changed safely."""


def plan_desktop(path: Path, entry: dict[str, Any]) -> DesktopPlan:
    try:
        before = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        before = ""
    except OSError as exc:
        raise ClientConfigError(f"cannot read {path}: {exc.strerror}") from None
    if before.strip():
        try:
            data = json.loads(before)
        except ValueError as exc:
            raise ClientConfigError(
                f"{path} is not valid JSON ({exc}); fix or move it first, it will not be "
                "overwritten"
            ) from None
        if not isinstance(data, dict):
            raise ClientConfigError(
                f"{path} does not hold a JSON object; it will not be overwritten"
            )
    else:
        data = {}
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ClientConfigError(f"'mcpServers' in {path} is not an object; it will not be changed")
    previous = servers.get(SERVER_NAME)
    if previous == entry:
        return DesktopPlan(path, "unchanged", before, before, previous)
    updated = dict(data)
    updated["mcpServers"] = {**servers, SERVER_NAME: entry}
    after = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    return DesktopPlan(path, "replace" if previous is not None else "add", before, after, previous)


def apply_desktop(plan: DesktopPlan) -> Path | None:
    """Write the plan. Returns the backup's path (None if there was no file before)."""
    if plan.action == "unchanged":
        return None
    current = plan.path.read_text(encoding="utf-8-sig") if plan.path.exists() else ""
    if current != plan.before:
        raise ClientConfigError(f"{plan.path} changed since the plan was made; run setup again")
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if plan.path.exists():
        backup = backup_path(plan.path)
        shutil.copy2(plan.path, backup)
    temporary = plan.path.with_name(plan.path.name + ".uat-tmp")
    temporary.write_text(plan.after, encoding="utf-8", newline="\n")
    os.replace(temporary, plan.path)
    return backup


def desktop_entry(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


# Claude Code


def claude_cli() -> str | None:
    return shutil.which("claude")


def claude_code_settings_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".claude.json"


@dataclass(frozen=True)
class CodeRegistrations:
    user: dict[str, Any] | None
    """The user-scope entry, available in every project."""
    local: dict[str, dict[str, Any]]
    """Project path -> local-scope entry with the same name, which shadows the user one."""


def code_registrations(home: Path | None = None) -> CodeRegistrations:
    """Read (never write) Claude Code's registrations of this server."""
    try:
        data = json.loads(claude_code_settings_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CodeRegistrations(None, {})
    if not isinstance(data, dict):
        return CodeRegistrations(None, {})
    servers = data.get("mcpServers")
    user = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    local: dict[str, dict[str, Any]] = {}
    projects = data.get("projects")
    if isinstance(projects, dict):
        for project, settings in projects.items():
            entries = settings.get("mcpServers") if isinstance(settings, dict) else None
            entry = entries.get(SERVER_NAME) if isinstance(entries, dict) else None
            if isinstance(entry, dict):
                local[str(project)] = entry
    return CodeRegistrations(user if isinstance(user, dict) else None, local)


def same_registration(entry: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    if entry is None:
        return False
    env = entry.get("env") or {}
    return bool(
        os.path.normcase(str(entry.get("command", ""))) == os.path.normcase(expected["command"])
        and list(entry.get("args") or []) == expected["args"]
        and env == expected["env"]
    )


def code_add_commands(entry: dict[str, Any], *, replace: bool) -> list[list[str]]:
    cli = claude_cli() or "claude"
    commands = []
    if replace:
        commands.append([cli, "mcp", "remove", "--scope", "user", SERVER_NAME])
    commands.append(
        [
            cli,
            "mcp",
            "add-json",
            "--scope",
            "user",
            SERVER_NAME,
            json.dumps({"type": "stdio", **entry}),
        ]
    )
    return commands


def code_remove_local_command() -> list[str]:
    return [claude_cli() or "claude", "mcp", "remove", "--scope", "local", SERVER_NAME]


def backup_path(path: Path, stamp: str | None = None) -> Path:
    """Where a client's settings file is copied before it is changed: ``<name>.bak-<time>``."""
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.name}.bak-{stamp}")


def backup_code_settings(home: Path | None = None) -> Path | None:
    """Copy ``~/.claude.json`` before ``claude mcp`` changes it. None if there is no file.

    The ``claude`` CLI writes that file itself and keeps no backup, so this is the only way
    back if a registration change goes wrong.
    """
    source = claude_code_settings_path(home)
    if not source.exists():
        return None
    target = backup_path(source)
    shutil.copy2(source, target)
    return target


def registration_problem(entry: dict[str, Any] | None) -> str | None:
    """Why a registration of this server is unsafe or broken, or None if it is not.

    The server refuses to start without ``-I`` (``server.check_isolated_start``), and an
    environment-mode registration has no approval and no audit log.
    """
    if entry is None:
        return None
    args = [str(arg) for arg in entry.get("args") or []]
    env = entry.get("env") or {}
    problems = []
    if "-I" not in args and "-P" not in args:
        problems.append("starts Python without -I, so the server refuses to start")
    if isinstance(env, dict) and ENV_CONFIG not in env:
        problems.append(
            "uses environment mode (no config file): read-only, no approval, no audit log"
        )
    return "; ".join(problems) or None


# The runtime: the installed copy of this package that the clients and the guard run.
#
# Not the checkout you develop in: the guard refuses Claude Code's changes to the code it runs
# (it could otherwise switch itself off), and a checkout's own code must stay editable. So
# ``install`` copies the package into a virtual environment in the state directory, which the
# guard and the server protect, and the registrations and the hook start that one. A change in
# the checkout takes effect only when you run ``install`` again.

RUNTIME_DIRECTORY_NAME = "runtime"
GUARD_MODULE = "universal_ai_tools.guard"
GUARD_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
GUARD_TIMEOUT_SECONDS = 60


def runtime_directory(home: Path | None = None) -> Path:
    from universal_ai_tools.settings import default_state_directory

    return default_state_directory(home) / RUNTIME_DIRECTORY_NAME


def runtime_python(directory: Path) -> Path:
    if sys.platform == "win32":
        return directory / "Scripts" / "python.exe"
    return directory / "bin" / "python"


def runtime_package(directory: Path) -> Path | None:
    """The ``universal_ai_tools`` directory installed in a runtime, or None."""
    patterns = (
        "Lib/site-packages/universal_ai_tools",
        "lib/python*/site-packages/universal_ai_tools",
    )
    for pattern in patterns:
        for found in sorted(directory.glob(pattern)):
            if found.is_dir():
                return found
    return None


def checkout_source() -> Path | None:
    """The project directory this package was installed from, if it is a checkout."""
    import universal_ai_tools

    root = Path(universal_ai_tools.__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() and (root / "src").is_dir() else None


def package_digest(package: Path) -> str:
    """A digest of a package directory's Python files (``__pycache__`` ignored)."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(package).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def install_commands(source: Path, directory: Path) -> list[list[str]]:
    """The commands that create or update the runtime from ``source``.

    A new runtime gets a virtual environment made by the base Python, then the package and its
    dependencies (``pip`` may download them). An existing one has the package's own files
    replaced (``--force-reinstall --no-deps``), which works while a server or hook is running
    from it: the package is pure Python, and the dependencies' compiled files are not touched
    unless a new dependency is needed.
    """
    python = str(runtime_python(directory))
    pip = [python, "-I", "-m", "pip", "--disable-pip-version-check", "--no-input", "install"]
    commands: list[list[str]] = []
    if not runtime_python(directory).exists():
        base = getattr(sys, "_base_executable", None) or sys.executable
        commands.append([base, "-I", "-m", "venv", str(directory)])
        commands.append([*pip, str(source)])
    else:
        commands.append([*pip, "--upgrade", str(source)])
        commands.append([*pip, "--force-reinstall", "--no-deps", str(source)])
    return commands


def run_command(command: list[str], *, timeout: float = 900) -> tuple[int, str]:
    """Run an installation command the user approved. Exit code and combined output."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argument lists, no shell
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return completed.returncode, (completed.stdout + completed.stderr).strip()


@dataclass(frozen=True)
class HookRun:
    code: int
    stdout: str
    stderr: str


def run_guard_handler(handler: dict[str, Any], event: dict[str, Any]) -> HookRun:
    """Run an installed guard handler the way Claude Code does: the event as JSON on stdin."""
    try:
        completed = subprocess.run(  # noqa: S603 - the hook's own exec-form command, no shell
            [str(handler["command"]), *[str(a) for a in handler.get("args", [])]],
            input=json.dumps(event).encode(),
            capture_output=True,
            timeout=float(handler.get("timeout", GUARD_TIMEOUT_SECONDS)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return HookRun(-1, "", f"{type(exc).__name__}: {exc}")
    return HookRun(
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )


# The Claude Code guard: hooks in ~/.claude/settings.json (see universal_ai_tools.guard).


def claude_user_settings_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".claude" / "settings.json"


def guard_handler(python: str, config_path: Path) -> dict[str, Any]:
    """One hook handler, in exec form (``args``): Claude Code starts Python directly, with no
    shell to quote paths for, and with ``-I`` so nothing in the project can replace the
    guard's code."""
    return {
        "type": "command",
        "command": python,
        "args": ["-I", "-m", GUARD_MODULE, "--config", str(config_path)],
        "timeout": GUARD_TIMEOUT_SECONDS,
    }


def guard_hooks(python: str, config_path: Path) -> dict[str, list[dict[str, Any]]]:
    """The hook entries ``setup`` adds: every tool, before (blocking if the guard cannot run)
    and after."""
    handler = guard_handler(python, config_path)
    return {
        "PreToolUse": [{"matcher": "*", "hooks": [{**handler, "onFailure": "block"}]}],
        "PostToolUse": [{"matcher": "*", "hooks": [handler]}],
        "PostToolUseFailure": [{"matcher": "*", "hooks": [handler]}],
    }


def is_guard_handler(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    args = handler.get("args")
    command = str(handler.get("command", ""))
    return (isinstance(args, list) and GUARD_MODULE in args) or GUARD_MODULE in command


@dataclass(frozen=True)
class HooksPlan:
    path: Path
    action: Literal["add", "replace", "unchanged"]
    before: str
    after: str

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.before.splitlines(keepends=True),
                self.after.splitlines(keepends=True),
                fromfile=f"{self.path} (now)",
                tofile=f"{self.path} (after)",
            )
        )


def _settings_object(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        before = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return "", {}
    except OSError as exc:
        raise ClientConfigError(f"cannot read {path}: {exc.strerror}") from None
    if not before.strip():
        return before, {}
    try:
        data = json.loads(before)
    except ValueError as exc:
        raise ClientConfigError(
            f"{path} is not valid JSON ({exc}); fix it first, it will not be overwritten"
        ) from None
    if not isinstance(data, dict):
        raise ClientConfigError(f"{path} does not hold a JSON object; it will not be changed")
    return before, data


def plan_guard_hooks(path: Path, python: str, config_path: Path) -> HooksPlan:
    """Add the guard's hooks to Claude Code's user settings, replacing older guard entries.

    Every other setting, and every other hook, is kept as it is."""
    before, data = _settings_object(path)
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ClientConfigError(f"'hooks' in {path} is not an object; it will not be changed")
    wanted = guard_hooks(python, config_path)
    updated_hooks: dict[str, Any] = {}
    found_old = False
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ClientConfigError(f"'hooks.{event}' in {path} is not a list")
        kept = []
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                kept.append(group)
                continue
            others = [h for h in handlers if not is_guard_handler(h)]
            found_old |= len(others) != len(handlers)
            if others:
                kept.append({**group, "hooks": others})
            elif not handlers:
                kept.append(group)
        updated_hooks[event] = kept
    for event, groups in wanted.items():
        updated_hooks[event] = [*updated_hooks.get(event, []), *groups]
    updated = dict(data)
    updated["hooks"] = updated_hooks
    if updated == data:
        return HooksPlan(path, "unchanged", before, before)
    after = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    return HooksPlan(path, "replace" if found_old else "add", before, after)


def _has_guard(group: object) -> bool:
    handlers = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(handlers, list) and any(is_guard_handler(h) for h in handlers)


def plan_remove_guard_hooks(path: Path) -> HooksPlan:
    """Take the guard's hooks out of Claude Code's user settings, keeping everything else."""
    before, data = _settings_object(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not any(
        _has_guard(g) for gs in hooks.values() if isinstance(gs, list) for g in gs
    ):
        return HooksPlan(path, "unchanged", before, before)
    updated_hooks: dict[str, Any] = {}
    for event, groups in hooks.items():
        kept = []
        for group in groups if isinstance(groups, list) else []:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(handlers, list) and any(is_guard_handler(h) for h in handlers):
                others = [h for h in handlers if not is_guard_handler(h)]
                if others:
                    kept.append({**group, "hooks": others})
                continue
            kept.append(group)
        if kept:
            updated_hooks[event] = kept
    updated = dict(data)
    if updated_hooks:
        updated["hooks"] = updated_hooks
    else:
        del updated["hooks"]
    after = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    return HooksPlan(path, "replace", before, after)


def apply_hooks(plan: HooksPlan) -> Path | None:
    """Write the plan atomically after copying the file. Returns the backup (None if new)."""
    if plan.action == "unchanged":
        return None
    current = plan.path.read_text(encoding="utf-8-sig") if plan.path.exists() else ""
    if current != plan.before:
        raise ClientConfigError(f"{plan.path} changed since the plan was made; run it again")
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if plan.path.exists():
        backup = backup_path(plan.path)
        shutil.copy2(plan.path, backup)
    temporary = plan.path.with_name(plan.path.name + ".uat-tmp")
    temporary.write_text(plan.after, encoding="utf-8", newline="\n")
    os.replace(temporary, plan.path)
    return backup


def installed_guard(path: Path) -> list[dict[str, Any]]:
    """Every guard handler in Claude Code's user settings, with its event (``_event``)."""
    try:
        _, data = _settings_object(path)
    except ClientConfigError:
        return []
    hooks = data.get("hooks")
    found: list[dict[str, Any]] = []
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            for group in groups if isinstance(groups, list) else []:
                handlers = group.get("hooks") if isinstance(group, dict) else None
                for handler in handlers if isinstance(handlers, list) else []:
                    if is_guard_handler(handler):
                        found.append({**handler, "_event": event, "_matcher": group.get("matcher")})
    return found


def run_claude(command: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Run a ``claude`` CLI command. Returns its exit code and combined output."""
    try:
        completed = subprocess.run(  # noqa: S603 - the user's own claude CLI, no shell
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def display(command: list[str]) -> str:
    """A command line as the user would type it."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(command)
    import shlex

    return shlex.join(command)

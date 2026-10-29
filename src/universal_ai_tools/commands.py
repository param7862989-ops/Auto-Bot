"""Bounded execution of fixed, operator-allowlisted developer command profiles."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from universal_ai_tools.config import CommandName
from universal_ai_tools.workspace import Workspace

MAX_TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 64 * 1024


class CommandError(ValueError):
    """A command request was refused or could not be run safely."""


class CommandResult(BaseModel):
    """Bounded result from one allowlisted command profile."""

    model_config = ConfigDict(frozen=True)

    command: CommandName
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool


def _command_argv(command: CommandName) -> list[str]:
    """Resolve a profile to a fixed argv vector; caller input is never an argument."""
    if command == "pytest":
        return [sys.executable, "-m", "pytest", "--no-header", "-q"]
    if command == "ruff-check":
        return [sys.executable, "-m", "ruff", "check", "."]
    if command == "mypy-check":
        return [sys.executable, "-m", "mypy", "src", "tests"]
    if command == "git-status":
        executable = shutil.which("git")
        if executable is None:
            raise CommandError("git is not available on PATH")
        return [executable, "status", "--short", "--branch"]
    if command == "git-diff-check":
        executable = shutil.which("git")
        if executable is None:
            raise CommandError("git is not available on PATH")
        return [executable, "diff", "--check"]
    raise CommandError("command profile is not supported")


def _minimal_environment() -> dict[str, str]:
    """Pass only process-launch essentials, not API keys or the parent's environment."""
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _drain(stream: object, target: bytearray, truncated: list[bool]) -> None:
    pipe = stream
    while True:
        chunk = pipe.read(8192)  # type: ignore[attr-defined]
        if not chunk:
            return
        room = MAX_OUTPUT_BYTES - len(target)
        if room > 0:
            target.extend(chunk[:room])
        if len(chunk) > room:
            truncated[0] = True


def _execute(
    argv: list[str], cwd: Path, timeout_seconds: int
) -> tuple[int | None, str, str, bool, bool]:
    env = _minimal_environment()
    if os.name == "nt":
        env.setdefault("SYSTEMROOT", str(Path(os.environ.get("SYSTEMROOT", "C:\\Windows"))))
    try:
        # argv comes only from _command_argv; shell parsing and user arguments are absent.
        process = subprocess.Popen(  # noqa: S603
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
        )
    except OSError:
        raise CommandError("command could not be started") from None

    stdout = bytearray()
    stderr = bytearray()
    truncation = [False]
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, stdout, truncation), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr, truncation), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while process.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            process.kill()
            break
        time.sleep(0.02)
    process.wait()
    for reader in readers:
        reader.join(timeout=2)
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()

    def decode(value: bytearray) -> str:
        return value.decode("utf-8", errors="replace")

    return process.returncode, decode(stdout), decode(stderr), timed_out, truncation[0]


def run_command(
    workspace: Workspace,
    command: CommandName,
    *,
    allowed_commands: tuple[CommandName, ...],
    timeout_seconds: int = 30,
) -> CommandResult:
    """Run one exact command profile from the authorized workspace root."""
    if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise CommandError(f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}")
    argv = _command_argv(command)
    if command not in allowed_commands:
        raise CommandError("command profile is not enabled")
    exit_code, stdout, stderr, timed_out, truncated = _execute(
        argv, workspace.root, timeout_seconds
    )
    return CommandResult(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        output_truncated=truncated,
    )

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest

from universal_ai_tools.commands import (
    MAX_OUTPUT_BYTES,
    CommandError,
    _execute,
    run_command,
)
from universal_ai_tools.config import CommandName
from universal_ai_tools.workspace import Workspace


def test_command_profile_must_be_allowlisted(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="not enabled"):
        run_command(Workspace(tmp_path), "pytest", allowed_commands=())


def test_profile_does_not_accept_caller_arguments(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="not supported"):
        run_command(
            Workspace(tmp_path),
            cast(CommandName, "python -c print('bad')"),
            allowed_commands=("pytest",),
        )


@pytest.mark.parametrize("timeout", [0, 121])
def test_timeout_is_bounded(tmp_path: Path, timeout: int) -> None:
    with pytest.raises(CommandError, match="timeout_seconds"):
        run_command(
            Workspace(tmp_path),
            "pytest",
            allowed_commands=("pytest",),
            timeout_seconds=timeout,
        )


def test_process_output_is_capped(tmp_path: Path) -> None:
    code = f"print('x' * {MAX_OUTPUT_BYTES + 1024})"
    exit_code, stdout, stderr, timed_out, truncated = _execute(
        [sys.executable, "-c", code], tmp_path, 10
    )

    assert exit_code == 0
    assert len(stdout.encode()) == MAX_OUTPUT_BYTES
    assert stderr == ""
    assert not timed_out
    assert truncated


def test_process_times_out(tmp_path: Path) -> None:
    exit_code, _, _, timed_out, _ = _execute(
        [sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, 1
    )

    assert exit_code is not None
    assert timed_out

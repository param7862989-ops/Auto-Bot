"""Helpers for tests of the server in file mode: config files, fake approval dialogs."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

from universal_ai_tools.approval import DialogAnswer, DialogHandle, DisplayLimits, ShowDialog
from universal_ai_tools.settings import ServiceConfig, load_config_file


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(type(value))


def write_config(
    directory: Path, workspace: Path, sections: dict[str, dict[str, Any]] | None = None
) -> Path:
    """A config file in ``directory`` (outside the workspace) for ``workspace``."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["version = 1", f"workspace = {toml_value(str(workspace))}"]
    for section, values in (sections or {}).items():
        lines.append(f"[{section}]")
        lines += [f"{key} = {toml_value(value)}" for key, value in values.items()]
    path = directory / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def service(
    directory: Path, workspace: Path, sections: dict[str, dict[str, Any]] | None = None
) -> ServiceConfig:
    return load_config_file(write_config(directory, workspace, sections))


class Dialog:
    """A stand-in for the Windows approval dialog that answers as told and records questions."""

    def __init__(self, answer: DialogAnswer = "yes") -> None:
        self.answer: DialogAnswer = answer
        self.asked: list[str] = []

    def __call__(self, text: str, handle: DialogHandle) -> DialogAnswer:
        self.asked.append(text)
        return self.answer


# A fixed size, between what limits_for_screen gives 1920x1080 screens at 100% and 125%,
# so that tests do not depend on the screen of the machine running them.
TEST_DIALOG_LIMITS = DisplayLimits(
    "the Windows approval dialog", width=76, max_lines=40, max_chars=3040
)


class DialogKwargs(TypedDict):
    show_dialog: ShowDialog
    dialog_available: Callable[[], bool]
    dialog_limits: DisplayLimits


def dialog_server_kwargs(
    dialog: Dialog, limits: DisplayLimits = TEST_DIALOG_LIMITS
) -> DialogKwargs:
    return {"show_dialog": dialog, "dialog_available": lambda: True, "dialog_limits": limits}


PYTHON = sys.executable


def registry_with_terminal(config: ServiceConfig) -> Any:
    """``build_registry(config)`` plus ``run_command``, which it never registers.

    General command execution is disabled (``settings.TERMINAL_DISABLED``) and no client can
    reach it. Tests still use it, through this, to keep the gate's handling of a
    code-executing tool tested: approval, exact arguments, and audit.
    """
    from universal_ai_tools import terminal
    from universal_ai_tools.settings import TerminalSettings
    from universal_ai_tools.tools import ToolRegistry, build_registry
    from universal_ai_tools.workspace import Workspace

    settings = TerminalSettings(enabled=True, programs=(PYTHON,))
    workspace = Workspace(config.workspace_root)
    return ToolRegistry([*build_registry(config), *terminal.specs(workspace, settings)])

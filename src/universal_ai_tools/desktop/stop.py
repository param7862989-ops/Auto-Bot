"""The emergency stop for input: throw the mouse pointer into a corner of the main screen.

Before every input action, and every few characters while typing, the input tools look at the
pointer. If it is in a corner of the primary monitor (within ``CORNER_PIXELS``), all input
stops, in every server process, until you resume it yourself: ``universal-ai-tools input
resume`` in your own terminal. A pointer always reaches a corner by being pushed into it,
however fast, so this works without a keyboard hook (which would see every key you press).

The stop is a file, ``input-stopped`` in the state directory (next to the config file), so
that Claude Desktop's and Claude Code's server processes both see it and it survives a
restart. That directory is protected: no Universal AI Tools tool can delete the file, and the
Claude Code guard refuses Claude Code's own file tools there. If the file cannot be written,
the stop holds in memory for the life of the process.

Separately, a ``type_text`` stops (without latching) as soon as the pointer moves while it
types: you took the mouse. Clicks and scrolls are refused inside the corner areas, so the
tools' own pointer moves can never trip the stop.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from universal_ai_tools.desktop.backend import Rect

STOP_FILE_NAME = "input-stopped"
CORNER_PIXELS = 4
RESUME_COMMAND = "universal-ai-tools input resume"


def stop_file(state_directory: Path) -> Path:
    return state_directory / STOP_FILE_NAME


def in_corner(x: int, y: int, screen: Rect, margin: int = CORNER_PIXELS) -> bool:
    """Whether a point is within ``margin`` pixels of a corner of ``screen``."""
    near_left = x <= screen.left + margin - 1
    near_right = x >= screen.right - margin
    near_top = y <= screen.top + margin - 1
    near_bottom = y >= screen.bottom - margin
    return (near_left or near_right) and (near_top or near_bottom)


class EmergencyStop:
    """Whether input is stopped, and the means to stop it. ``path`` None: memory only."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._memory: str | None = None

    @property
    def path(self) -> Path | None:
        return self._path

    def reason(self) -> str | None:
        """Why input is stopped, or None if it is not."""
        if self._memory is not None:
            return self._memory
        if self._path is None:
            return None
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return "the stop file exists but cannot be read"
        try:
            data = json.loads(text)
            return str(data.get("reason", "stopped"))
        except (ValueError, AttributeError):
            return "stopped"

    def trigger(self, reason: str) -> None:
        """Stop all input until the user resumes it."""
        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                record = {"reason": reason, "time": time.time(), "pid": os.getpid()}
                self._path.write_text(json.dumps(record) + "\n", encoding="utf-8")
                return
            except OSError:
                pass
        self._memory = reason

    def message(self) -> str:
        reason = self.reason() or "stopped"
        return (
            f"input is stopped ({reason}). Nothing was sent. Only you can resume it: run "
            f"'{RESUME_COMMAND}' in your own terminal."
        )


__all__ = ["CORNER_PIXELS", "STOP_FILE_NAME", "EmergencyStop", "in_corner", "stop_file"]

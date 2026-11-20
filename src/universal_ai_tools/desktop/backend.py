"""What the desktop tools need from the operating system.

``tools.py`` makes every safety decision; a backend only reports facts and performs the
low-level action it is told to. Tests use a fake backend, so no test sends real input.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

MouseButton = Literal["left", "right", "middle"]


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@dataclass(frozen=True)
class WindowInfo:
    window_id: int
    title: str
    process_name: str
    """The executable's file name, lower case, e.g. ``notepad.exe``; empty if unknown."""
    pid: int
    rect: Rect
    minimized: bool
    class_name: str
    elevated: bool | None
    """Whether the window's process runs elevated; None if that could not be read."""


@dataclass(frozen=True)
class Bitmap:
    width: int
    height: int
    bgra: bytes


ElementAction = Literal["invoke", "toggle", "select", "expand", "collapse", "set_value", "focus"]


@dataclass(frozen=True)
class ElementInfo:
    """One control of a window, as UI Automation reports it. All text is untrusted."""

    handle: int
    """The backend's reference to the live element, valid until forgotten."""
    depth: int
    """1 for the window's direct children."""
    control_type: str
    """``button``, ``edit``, ``menu item``, ..."""
    name: str
    automation_id: str
    class_name: str
    enabled: bool
    offscreen: bool
    password: bool
    value: str | None
    """The text of a value control (never of a password field), or None."""
    actions: tuple[ElementAction, ...]
    rect: Rect
    process_id: int
    readable: bool = False
    """Its full text can be read (``read_text``): a document or text field."""
    table: bool = False
    """It is a grid or table (``read_table``)."""


@dataclass(frozen=True)
class TableData:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    total_rows: int
    total_columns: int


class DesktopBackend(Protocol):
    def list_windows(self) -> list[WindowInfo]:
        """Visible, titled top-level windows, front to back."""
        ...

    def window(self, window_id: int) -> WindowInfo | None:
        """The window, if it still exists and is visible."""
        ...

    def foreground_window(self) -> int:
        """The id of the foreground window, or 0."""
        ...

    def window_at(self, x: int, y: int) -> int:
        """The top-level window under a screen point, or 0."""
        ...

    def screen_rect(self) -> Rect:
        """The virtual screen: all monitors, in physical pixels."""
        ...

    def capture(self, region: Rect, width: int, height: int) -> Bitmap:
        """The screen region, scaled to ``width`` x ``height``."""
        ...

    def focus(self, window_id: int) -> bool:
        """Try to bring the window to the foreground. True if it is there afterwards."""
        ...

    def click(self, x: int, y: int, button: MouseButton, count: int) -> None: ...

    def scroll(self, x: int, y: int, clicks: int, horizontal: bool) -> None: ...

    def type_text(self, text: str, keep_going: Callable[[], bool]) -> int:
        """Type ``text``; stop as soon as ``keep_going`` is false. Returns characters typed."""
        ...

    def press_keys(self, keys: list[int]) -> None:
        """Press the virtual keys in order, then release them in reverse order."""
        ...

    def approval_dialog_open(self) -> bool:
        """Whether any process shows a Universal AI Tools approval dialog."""
        ...

    def input_lock(self) -> Callable[[], None] | None:
        """Take the machine-wide input lock; returns its release, or None if busy."""
        ...

    def own_process_id(self) -> int: ...

    def cursor_position(self) -> tuple[int, int]:
        """Where the mouse pointer is, in screen pixels."""
        ...

    def primary_screen(self) -> Rect:
        """The primary monitor, whose corners are the emergency stop."""
        ...

    def inspect_window(self, window_id: int, limit: int) -> tuple[list[ElementInfo], bool]:
        """The window's controls (UI Automation control view), and whether there were more.

        Raises:
            OSError: If UI Automation is unavailable or the application does not answer.
        """
        ...

    def element_state(self, handle: int) -> ElementInfo | None:
        """The element as it is now, or None if it no longer exists."""
        ...

    def element_action(self, handle: int, action: ElementAction, value: str | None) -> bool:
        """Perform the action. True if finished; False if still running after a short wait.

        Raises:
            OSError: If the element is gone or does not support the action.
        """
        ...

    def forget_elements(self, handles: list[int]) -> None: ...

    def element_text(self, handle: int, limit: int) -> str:
        """An element's whole text (text or value pattern), at most ``limit`` characters.

        Raises:
            OSError: If it is gone, has no text, or is a password field.
        """
        ...

    def element_table(self, handle: int, max_rows: int, max_columns: int) -> TableData:
        """A grid or table element's cells (their names, or values), and its headers.

        Raises:
            OSError: If it is gone or is not a grid.
        """
        ...

    def focused_text(self) -> str | None:
        """The text of the focused control (UI Automation value), or None if it has none,
        is a password field, or cannot be read. Used to check what typing produced."""
        ...

    def start_overlay(self, text: Callable[[], str | None], on_hotkey: Callable[[], None]) -> None:
        """Show ``text()`` in an on-screen banner while it is not None (polled about once a
        second), and call ``on_hotkey`` when the stop hotkey is pressed. Best effort."""
        ...

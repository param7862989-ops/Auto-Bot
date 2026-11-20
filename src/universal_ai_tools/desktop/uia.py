"""Windows UI Automation: a window's controls by name and role, and their actions.

UI Automation is the accessibility interface screen readers use. Through it a client can list
a window's buttons, fields, menus, and list items with their names, and press a button, tick
a box, select an item, or set a field's text without moving the mouse or pressing keys. That
is more reliable than clicking pixels, and the approval can name the exact control.

Every call goes to the target application's process and can take as long as that application
takes to answer; a hung application, or a button whose handler opens a modal dialog, can
block a call indefinitely. So each call runs on its own daemon thread and is waited for at
most a fixed time (``CALL_TIMEOUT_SECONDS``): a call that does not finish in time is reported
as such, and the server goes on. All threads join COM's multithreaded apartment, which one
thread here keeps alive for the life of the process, so element references can be used from
any of them. ``IUIAutomation2`` connection and transaction timeouts are set as well where
Windows offers them.

Uses ``comtypes``, which generates the interface wrappers from Windows' own type library
(``UIAutomationCore.dll``) on first use.
"""

from __future__ import annotations

import itertools
import re
import sys
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from universal_ai_tools.desktop.backend import ElementAction, ElementInfo, Rect, TableData

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("UI Automation needs Windows")

CALL_TIMEOUT_SECONDS = 10.0
ACTION_TIMEOUT_SECONDS = 5.0
MAX_DEPTH = 30
MAX_TEXT = 500

T = TypeVar("T")


class UIAError(OSError):
    """UI Automation could not do what was asked."""


class UIATimeout(UIAError):
    """The application did not answer in time."""


class UIAutomation:
    """The UI Automation client of this process. Create it once; it is thread-safe."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._automation: Any = None
        self._uia: Any = None
        self._walker: Any = None
        self._elements: dict[int, Any] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._types: dict[int, str] = {}
        thread = threading.Thread(target=self._keep_apartment, name="uia-apartment", daemon=True)
        thread.start()
        if not self._ready.wait(30):
            raise UIATimeout("UI Automation did not start")
        if self._error is not None:
            raise UIAError(f"UI Automation is not available: {self._error}") from None

    def _keep_apartment(self) -> None:
        try:
            # Join the multithreaded apartment, here and in comtypes' own import-time setup.
            sys.coinit_flags = 0  # type: ignore[attr-defined,unused-ignore]
            import comtypes
            import comtypes.client

            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as uia

            automation = comtypes.client.CreateObject(
                uia.CUIAutomation, interface=uia.IUIAutomation
            )
            try:
                second = automation.QueryInterface(uia.IUIAutomation2)
                second.ConnectionTimeout = int(CALL_TIMEOUT_SECONDS * 1000 / 2)
                second.TransactionTimeout = int(CALL_TIMEOUT_SECONDS * 1000 / 2)
            except Exception:  # noqa: S110 - older Windows: no IUIAutomation2, no timeouts
                pass
            self._uia = uia
            self._automation = automation
            self._walker = automation.ControlViewWalker
            pattern = re.compile(r"^UIA_(\w+)ControlTypeId$")
            for name in dir(uia):
                found = pattern.match(name)
                if found:
                    self._types[int(getattr(uia, name))] = _words(found.group(1))
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        threading.Event().wait()  # keeps the apartment, and the objects in it, alive

    def _call(self, function: Callable[[], T], timeout: float = CALL_TIMEOUT_SECONDS) -> T:
        box: list[tuple[bool, Any]] = []
        done = threading.Event()

        def run() -> None:
            import comtypes

            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            try:
                box.append((True, function()))
            except BaseException as exc:
                box.append((False, exc))
            finally:
                done.set()

        threading.Thread(target=run, name="uia-call", daemon=True).start()
        if not done.wait(timeout):
            raise UIATimeout(
                f"the application did not answer within {timeout:g} seconds (it may be busy, "
                "hung, or showing a dialog)"
            )
        ok, value = box[0]
        if not ok:
            if isinstance(value, UIAError):
                raise value
            raise UIAError(f"UI Automation failed: {type(value).__name__}: {value}") from None
        return value  # type: ignore[no-any-return,unused-ignore]

    # Reading.

    def inspect(self, window_id: int, limit: int) -> tuple[list[ElementInfo], bool]:
        """The window's controls, depth first in the control view, at most ``limit``."""

        def run() -> tuple[list[ElementInfo], bool]:
            root = self._automation.ElementFromHandle(window_id)
            found: list[ElementInfo] = []
            truncated = False
            stack: list[tuple[Any, int]] = [(root, 0)]
            while stack:
                element, depth = stack.pop()
                if depth > 0:
                    if len(found) >= limit:
                        truncated = True
                        break
                    found.append(self._describe(element, depth, self._remember(element)))
                if depth >= MAX_DEPTH:
                    continue
                children: list[Any] = []
                child = self._walker.GetFirstChildElement(element)
                while child and len(children) < limit:
                    children.append(child)
                    child = self._walker.GetNextSiblingElement(child)
                stack += [(c, depth + 1) for c in reversed(children)]
            return found, truncated

        return self._call(run)

    def state(self, handle: int) -> ElementInfo | None:
        """The element's properties now, or None if it is gone."""
        element = self._elements.get(handle)
        if element is None:
            return None

        def run() -> ElementInfo | None:
            try:
                return self._describe(element, 0, handle)
            except Exception:
                return None

        return self._call(run)

    def text(self, handle: int, limit: int) -> str:
        """The element's whole text: its text pattern's document, or its value."""
        element = self._live(handle)
        uia = self._uia

        def run() -> str:
            if element.CurrentIsPassword:
                raise UIAError("password fields are never read")
            found = element.GetCurrentPattern(uia.UIA_TextPatternId)
            if found:
                pattern = found.QueryInterface(uia.IUIAutomationTextPattern)
                return str(pattern.DocumentRange.GetText(limit) or "")
            found = element.GetCurrentPattern(uia.UIA_ValuePatternId)
            if found:
                value = found.QueryInterface(uia.IUIAutomationValuePattern).CurrentValue
                return str(value or "")[:limit]
            raise UIAError("the element has no text to read")

        return self._call(run)

    def table(self, handle: int, max_rows: int, max_columns: int) -> TableData:
        """The grid's cells, row by row, and its column headers if it has them."""
        element = self._live(handle)
        uia = self._uia

        def cell_text(cell: Any) -> str:
            if not cell:
                return ""
            if cell.CurrentIsPassword:
                return ""
            found = cell.GetCurrentPattern(uia.UIA_ValuePatternId)
            if found:
                value = found.QueryInterface(uia.IUIAutomationValuePattern).CurrentValue
                if value:
                    return str(value)[:MAX_TEXT]
            return str(cell.CurrentName or "")[:MAX_TEXT]

        def run() -> TableData:
            found = element.GetCurrentPattern(uia.UIA_GridPatternId)
            if not found:
                raise UIAError("the element is not a table or grid")
            grid = found.QueryInterface(uia.IUIAutomationGridPattern)
            total_rows = int(grid.CurrentRowCount)
            total_columns = int(grid.CurrentColumnCount)
            columns = min(total_columns, max_columns)
            headers: tuple[str, ...] = ()
            table_pattern = element.GetCurrentPattern(uia.UIA_TablePatternId)
            if table_pattern:
                array = table_pattern.QueryInterface(
                    uia.IUIAutomationTablePattern
                ).GetCurrentColumnHeaders()
                if array:
                    headers = tuple(
                        cell_text(array.GetElement(i)) for i in range(min(array.Length, columns))
                    )
            rows = tuple(
                tuple(cell_text(grid.GetItem(row, column)) for column in range(columns))
                for row in range(min(total_rows, max_rows))
            )
            return TableData(headers, rows, total_rows, total_columns)

        return self._call(run, CALL_TIMEOUT_SECONDS * 3)

    def _live(self, handle: int) -> Any:
        element = self._elements.get(handle)
        if element is None:
            raise UIAError("that element is no longer known; inspect the window again")
        return element

    def focused_text(self, limit: int) -> str | None:
        """The text of the control with the keyboard focus, if it has a value (never for a
        password field); None if there is none or it cannot be read."""
        uia = self._uia

        def run() -> str | None:
            element = self._automation.GetFocusedElement()
            if not element or element.CurrentIsPassword:
                return None
            found = element.GetCurrentPattern(uia.UIA_ValuePatternId)
            if not found:
                return None
            value = found.QueryInterface(uia.IUIAutomationValuePattern).CurrentValue
            return str(value or "")[:limit]

        try:
            return self._call(run)
        except UIAError:
            return None

    def forget(self, handles: list[int]) -> None:
        with self._lock:
            for handle in handles:
                self._elements.pop(handle, None)

    def _remember(self, element: Any) -> int:
        with self._lock:
            handle = next(self._ids)
            self._elements[handle] = element
            return handle

    def _describe(self, element: Any, depth: int, handle: int) -> ElementInfo:
        uia = self._uia
        password = bool(element.CurrentIsPassword)
        actions: list[ElementAction] = []
        value: str | None = None
        for action, pattern_id in (
            ("invoke", uia.UIA_InvokePatternId),
            ("toggle", uia.UIA_TogglePatternId),
            ("select", uia.UIA_SelectionItemPatternId),
            ("expand", uia.UIA_ExpandCollapsePatternId),
        ):
            if element.GetCurrentPattern(pattern_id):
                actions.append(action)  # type: ignore[arg-type,unused-ignore]
        if "expand" in actions:
            actions.append("collapse")
        value_pattern = element.GetCurrentPattern(uia.UIA_ValuePatternId)
        if value_pattern:
            value_pattern = value_pattern.QueryInterface(uia.IUIAutomationValuePattern)
            if not password:
                value = str(value_pattern.CurrentValue or "")[:MAX_TEXT]
            if not value_pattern.CurrentIsReadOnly:
                actions.append("set_value")
        if element.CurrentIsKeyboardFocusable:
            actions.append("focus")
        rect = element.CurrentBoundingRectangle
        readable = not password and bool(
            element.GetCurrentPattern(uia.UIA_TextPatternId) or value is not None
        )
        table = bool(element.GetCurrentPattern(uia.UIA_GridPatternId))
        return ElementInfo(
            readable=readable,
            table=table,
            handle=handle,
            depth=depth,
            control_type=self._types.get(int(element.CurrentControlType), "unknown"),
            name=str(element.CurrentName or "")[:MAX_TEXT],
            automation_id=str(element.CurrentAutomationId or "")[:200],
            class_name=str(element.CurrentClassName or "")[:200],
            enabled=bool(element.CurrentIsEnabled),
            offscreen=bool(element.CurrentIsOffscreen),
            password=password,
            value=value,
            actions=tuple(actions),
            rect=Rect(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
            process_id=int(element.CurrentProcessId),
        )

    # Acting.

    def act(self, handle: int, action: ElementAction, value: str | None) -> bool:
        """Perform ``action``. True if it finished; False if the application is still busy
        with it after ``ACTION_TIMEOUT_SECONDS`` (it was sent, and may still take effect).

        Raises:
            UIAError: If the element is gone, or does not support the action.
        """
        element = self._elements.get(handle)
        if element is None:
            raise UIAError("that element is no longer known; inspect the window again")
        uia = self._uia

        def pattern(pattern_id: int, interface: Any) -> Any:
            found = element.GetCurrentPattern(pattern_id)
            if not found:
                raise UIAError(f"the element does not support '{action}'")
            return found.QueryInterface(interface)

        def run() -> None:
            if action == "invoke":
                pattern(uia.UIA_InvokePatternId, uia.IUIAutomationInvokePattern).Invoke()
            elif action == "toggle":
                pattern(uia.UIA_TogglePatternId, uia.IUIAutomationTogglePattern).Toggle()
            elif action == "select":
                pattern(
                    uia.UIA_SelectionItemPatternId, uia.IUIAutomationSelectionItemPattern
                ).Select()
            elif action == "expand":
                pattern(
                    uia.UIA_ExpandCollapsePatternId, uia.IUIAutomationExpandCollapsePattern
                ).Expand()
            elif action == "collapse":
                pattern(
                    uia.UIA_ExpandCollapsePatternId, uia.IUIAutomationExpandCollapsePattern
                ).Collapse()
            elif action == "set_value":
                pattern(uia.UIA_ValuePatternId, uia.IUIAutomationValuePattern).SetValue(value or "")
            elif action == "focus":
                element.SetFocus()
            else:
                raise UIAError(f"unknown action '{action}'")

        try:
            self._call(run, ACTION_TIMEOUT_SECONDS)
        except UIATimeout:
            return False
        return True


def _words(camel: str) -> str:
    """``SplitButton`` -> ``split button``."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", camel).lower()

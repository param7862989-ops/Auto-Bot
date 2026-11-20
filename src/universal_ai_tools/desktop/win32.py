"""The desktop backend for Windows, through ``user32``, ``gdi32``, and ``dwmapi`` via ctypes.

The process is made per-monitor DPI aware when this backend is created, so every
coordinate here is in physical screen pixels, and screenshots and clicks agree.

Input is sent with ``SendInput``, as if from the keyboard and mouse. Windows itself drops
input to a window of a more privileged (elevated) process; ``tools.py`` refuses such a
target up front instead of letting the input vanish. Nothing here can reach the secure
desktop (UAC prompts, Ctrl+Alt+Del, the lock screen).
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import TYPE_CHECKING, Any

from universal_ai_tools.approval import APPROVAL_DIALOG_TITLE
from universal_ai_tools.desktop.backend import (
    Bitmap,
    ElementAction,
    ElementInfo,
    MouseButton,
    Rect,
    TableData,
    WindowInfo,
)

if TYPE_CHECKING:
    from universal_ai_tools.desktop.overlay import Overlay
    from universal_ai_tools.desktop.uia import UIAutomation

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("the Windows desktop backend needs Windows")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi")
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

_ULONG_PTR = ctypes.c_size_t
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _sig(function: Any, argtypes: list[object], restype: object) -> None:
    function.argtypes = argtypes
    function.restype = restype


_sig(_user32.EnumWindows, [_WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)
_sig(_user32.IsWindow, [wintypes.HWND], wintypes.BOOL)
_sig(_user32.IsWindowVisible, [wintypes.HWND], wintypes.BOOL)
_sig(_user32.IsIconic, [wintypes.HWND], wintypes.BOOL)
_sig(_user32.GetWindowTextLengthW, [wintypes.HWND], ctypes.c_int)
_sig(_user32.GetWindowTextW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(_user32.GetClassNameW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(_user32.GetWindowRect, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL)
_sig(
    _user32.GetWindowThreadProcessId,
    [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
    wintypes.DWORD,
)
_sig(_user32.GetForegroundWindow, [], wintypes.HWND)
_sig(_user32.SetForegroundWindow, [wintypes.HWND], wintypes.BOOL)
_sig(_user32.BringWindowToTop, [wintypes.HWND], wintypes.BOOL)
_sig(_user32.ShowWindow, [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
_sig(_user32.WindowFromPoint, [wintypes.POINT], wintypes.HWND)
_sig(_user32.GetAncestor, [wintypes.HWND, wintypes.UINT], wintypes.HWND)
_sig(_user32.GetSystemMetrics, [ctypes.c_int], ctypes.c_int)
_sig(_user32.SetCursorPos, [ctypes.c_int, ctypes.c_int], wintypes.BOOL)
_sig(_user32.GetCursorPos, [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL)
_sig(_user32.GetDC, [wintypes.HWND], wintypes.HDC)
_sig(_user32.ReleaseDC, [wintypes.HWND, wintypes.HDC], ctypes.c_int)
_sig(_user32.AttachThreadInput, [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL)
_sig(_user32.FindWindowW, [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND)
_sig(_gdi32.CreateCompatibleDC, [wintypes.HDC], wintypes.HDC)
_sig(_gdi32.DeleteDC, [wintypes.HDC], wintypes.BOOL)
_sig(_gdi32.SelectObject, [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ)
_sig(_gdi32.DeleteObject, [wintypes.HGDIOBJ], wintypes.BOOL)
_sig(_gdi32.SetStretchBltMode, [wintypes.HDC, ctypes.c_int], ctypes.c_int)
_sig(
    _gdi32.SetBrushOrgEx, [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPVOID], wintypes.BOOL
)
_sig(
    _gdi32.StretchBlt,
    [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD],
    wintypes.BOOL,
)  # fmt: skip
_sig(
    _gdi32.CreateDIBSection,
    [wintypes.HDC, wintypes.LPVOID, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
     wintypes.HANDLE, wintypes.DWORD],
    wintypes.HBITMAP,
)  # fmt: skip
_sig(_kernel32.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
_sig(_kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
_sig(_kernel32.GetCurrentThreadId, [], wintypes.DWORD)
_sig(
    _kernel32.QueryFullProcessImageNameW,
    [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
    wintypes.BOOL,
)
_sig(_kernel32.CreateMutexW, [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE)
_sig(_kernel32.WaitForSingleObject, [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD)
_sig(_kernel32.ReleaseMutex, [wintypes.HANDLE], wintypes.BOOL)
_sig(
    _advapi32.OpenProcessToken,
    [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
    wintypes.BOOL,
)
_sig(
    _advapi32.GetTokenInformation,
    [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ],
    wintypes.BOOL,
)
_sig(
    _dwmapi.DwmGetWindowAttribute,
    [wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD],
    ctypes.c_long,
)


class _MouseInput(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _KeyboardInput(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _HardwareInput(ctypes.Structure):
    _fields_ = (("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD))


class _InputUnion(ctypes.Union):
    _fields_ = (("mi", _MouseInput), ("ki", _KeyboardInput), ("hi", _HardwareInput))


class _Input(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _InputUnion))


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


_sig(_user32.SendInput, [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int], wintypes.UINT)
_sig(_user32.GetKeyboardLayout, [wintypes.DWORD], wintypes.HKL)
_sig(_user32.VkKeyScanExW, [wintypes.WCHAR, wintypes.HKL], ctypes.c_short)
_sig(_user32.MapVirtualKeyExW, [wintypes.UINT, wintypes.UINT, wintypes.HKL], wintypes.UINT)
_sig(_user32.GetKeyState, [ctypes.c_int], ctypes.c_short)

_VK_SHIFT = 0x10
_VK_CAPITAL = 0x14
_MAPVK_VK_TO_VSC = 0


def _layout_keys() -> Callable[[str], tuple[int, int, bool] | None]:
    """How to type a character as a real key on the foreground window's keyboard layout:
    ``(virtual key, scan code, shift)``, or None to fall back to a Unicode key event.

    Real keys are what a physical keyboard sends; the application turns them into
    characters itself, so it cannot misread them however busy it is. Characters that need
    Ctrl or Alt (AltGr) on this layout, and every character while Caps Lock is on, go as
    Unicode key events instead.
    """
    foreground = _user32.GetForegroundWindow()
    thread = _user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    layout = _user32.GetKeyboardLayout(thread)
    caps = bool(_user32.GetKeyState(_VK_CAPITAL) & 1)

    def key(character: str) -> tuple[int, int, bool] | None:
        if caps or len(character) != 1 or ord(character) > 0xFFFF:
            return None
        found = _user32.VkKeyScanExW(character, layout)
        if found == -1:
            return None
        vk, modifiers = found & 0xFF, (found >> 8) & 0xFF
        if modifiers & ~0x1:  # needs Ctrl, Alt, or a layout-specific modifier
            return None
        scan = _user32.MapVirtualKeyExW(vk, _MAPVK_VK_TO_VSC, layout)
        return vk, scan, bool(modifiers & 0x1)

    return key


_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x2
_KEYEVENTF_UNICODE = 0x4
_KEYEVENTF_EXTENDEDKEY = 0x1
_MOUSE_FLAGS: dict[MouseButton, tuple[int, int]] = {
    "left": (0x2, 0x4),
    "right": (0x8, 0x10),
    "middle": (0x20, 0x40),
}
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000
_WHEEL_DELTA = 120
_VK_RETURN = 0x0D
_VK_TAB = 0x09
_VK_MENU = 0x12
_EXTENDED_KEYS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E})
_GA_ROOT = 2
_SW_RESTORE = 9
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20
_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_HALFTONE = 4
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_INPUT_MUTEX = "Local\\UniversalAITools.Input"
_KEY_GAP_SECONDS = 0.012
_CHARACTER_GAP_SECONDS = 0.025
_MAX_CHECKED_TEXT = 200_000


def _make_dpi_aware() -> None:
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        with contextlib.suppress(AttributeError, OSError):
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)


def _text(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(min(length, 4096) + 1)
    _user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _rect(hwnd: int) -> Rect:
    rect = wintypes.RECT()
    if _dwmapi.DwmGetWindowAttribute(
        hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
    ) != 0 and not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return Rect(0, 0, 0, 0)
    return Rect(rect.left, rect.top, rect.right, rect.bottom)


def _cloaked(hwnd: int) -> bool:
    value = wintypes.DWORD()
    result = _dwmapi.DwmGetWindowAttribute(
        hwnd, _DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
    )
    return result == 0 and value.value != 0


def _process(pid: int) -> tuple[str, bool | None]:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "", None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        name = ""
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            name = os.path.basename(buffer.value).casefold()
        token = wintypes.HANDLE()
        elevated: bool | None = None
        if _advapi32.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
            try:
                value = wintypes.DWORD()
                returned = wintypes.DWORD()
                if _advapi32.GetTokenInformation(
                    token, _TOKEN_ELEVATION, ctypes.byref(value), 4, ctypes.byref(returned)
                ):
                    elevated = bool(value.value)
            finally:
                _kernel32.CloseHandle(token)
        return name, elevated
    finally:
        _kernel32.CloseHandle(handle)


def _info(hwnd: int) -> WindowInfo:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name, elevated = _process(pid.value)
    return WindowInfo(
        window_id=int(hwnd),
        title=_text(hwnd),
        process_name=name,
        pid=pid.value,
        rect=_rect(hwnd),
        minimized=bool(_user32.IsIconic(hwnd)),
        class_name=_class_name(hwnd),
        elevated=elevated,
    )


def _send(inputs: list[_Input]) -> None:
    array = (_Input * len(inputs))(*inputs)
    sent = _user32.SendInput(len(inputs), array, ctypes.sizeof(_Input))
    if sent != len(inputs):
        raise OSError("SendInput was blocked (another program or Windows refused the input)")


def _key(vk: int = 0, *, scan: int = 0, up: bool = False, unicode: bool = False) -> _Input:
    flags = (_KEYEVENTF_KEYUP if up else 0) | (_KEYEVENTF_UNICODE if unicode else 0)
    if vk in _EXTENDED_KEYS:
        flags |= _KEYEVENTF_EXTENDEDKEY
    item = _Input(type=_INPUT_KEYBOARD)
    item.u.ki = _KeyboardInput(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


def _mouse(flags: int, data: int = 0) -> _Input:
    item = _Input(type=_INPUT_MOUSE)
    item.u.mi = _MouseInput(
        dx=0, dy=0, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0
    )
    return item


class Win32Backend:
    def __init__(self) -> None:
        _make_dpi_aware()
        self._mutex = _kernel32.CreateMutexW(None, False, _INPUT_MUTEX)
        self._uia: UIAutomation | None = None
        self._automation_lock = threading.Lock()
        self._overlay: Overlay | None = None

    def own_process_id(self) -> int:
        return os.getpid()

    def list_windows(self) -> list[WindowInfo]:
        found: list[int] = []

        def visit(hwnd: int, _lparam: int) -> bool:
            if (
                _user32.IsWindowVisible(hwnd)
                and _user32.GetWindowTextLengthW(hwnd) > 0
                and not _cloaked(hwnd)
            ):
                found.append(int(hwnd))
            return len(found) < 500

        _user32.EnumWindows(_WNDENUMPROC(visit), 0)
        return [_info(hwnd) for hwnd in found]

    def window(self, window_id: int) -> WindowInfo | None:
        if (
            not window_id
            or not _user32.IsWindow(window_id)
            or not _user32.IsWindowVisible(window_id)
        ):
            return None
        return _info(window_id)

    def foreground_window(self) -> int:
        return int(_user32.GetForegroundWindow() or 0)

    def window_at(self, x: int, y: int) -> int:
        hwnd = _user32.WindowFromPoint(wintypes.POINT(x, y))
        if not hwnd:
            return 0
        return int(_user32.GetAncestor(hwnd, _GA_ROOT) or 0)

    def screen_rect(self) -> Rect:
        left = _user32.GetSystemMetrics(76)
        top = _user32.GetSystemMetrics(77)
        return Rect(
            left, top, left + _user32.GetSystemMetrics(78), top + _user32.GetSystemMetrics(79)
        )

    def capture(self, region: Rect, width: int, height: int) -> Bitmap:
        screen = _user32.GetDC(None)
        if not screen:
            raise OSError("cannot read the screen (no desktop is available)")
        memory = _gdi32.CreateCompatibleDC(screen)
        header = _BitmapInfoHeader(
            biSize=ctypes.sizeof(_BitmapInfoHeader),
            biWidth=width,
            biHeight=-height,  # top-down rows
            biPlanes=1,
            biBitCount=32,
            biCompression=0,
        )
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(
            memory, ctypes.byref(header), 0, ctypes.byref(bits), None, 0
        )
        try:
            if not bitmap or not bits:
                raise OSError("cannot allocate the screenshot")
            previous = _gdi32.SelectObject(memory, bitmap)
            _gdi32.SetStretchBltMode(memory, _HALFTONE)
            _gdi32.SetBrushOrgEx(memory, 0, 0, None)
            ok = _gdi32.StretchBlt(
                memory, 0, 0, width, height,
                screen, region.left, region.top, region.width, region.height,
                _SRCCOPY | _CAPTUREBLT,
            )  # fmt: skip
            _gdi32.SelectObject(memory, previous)
            if not ok:
                raise OSError("the screen could not be copied (is the session locked?)")
            data = ctypes.string_at(bits, width * height * 4)
        finally:
            if bitmap:
                _gdi32.DeleteObject(bitmap)
            _gdi32.DeleteDC(memory)
            _user32.ReleaseDC(None, screen)
        return Bitmap(width, height, data)

    def focus(self, window_id: int) -> bool:
        if _user32.IsIconic(window_id):
            _user32.ShowWindow(window_id, _SW_RESTORE)
        foreground = _user32.GetForegroundWindow()
        ours = _kernel32.GetCurrentThreadId()
        theirs = _user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = bool(theirs and theirs != ours and _user32.AttachThreadInput(ours, theirs, True))
        try:
            _user32.BringWindowToTop(window_id)
            done = _user32.SetForegroundWindow(window_id)
        finally:
            if attached:
                _user32.AttachThreadInput(ours, theirs, False)
        if not done:
            # Windows lets a process take the foreground right after a key event it sent.
            _send([_key(_VK_MENU), _key(_VK_MENU, up=True)])
            _user32.SetForegroundWindow(window_id)
        for _ in range(20):
            if int(_user32.GetForegroundWindow() or 0) == window_id:
                return True
            time.sleep(0.025)
        return False

    def click(self, x: int, y: int, button: MouseButton, count: int) -> None:
        if not _user32.SetCursorPos(x, y):
            raise OSError("the pointer could not be moved")
        down, up = _MOUSE_FLAGS[button]
        events = []
        for _ in range(count):
            events += [_mouse(down), _mouse(up)]
        _send(events)

    def scroll(self, x: int, y: int, clicks: int, horizontal: bool) -> None:
        if not _user32.SetCursorPos(x, y):
            raise OSError("the pointer could not be moved")
        flag = _MOUSEEVENTF_HWHEEL if horizontal else _MOUSEEVENTF_WHEEL
        _send([_mouse(flag, clicks * _WHEEL_DELTA)])

    def type_text(self, text: str, keep_going: Callable[[], bool]) -> int:
        typed = 0
        layout_key = _layout_keys()
        for index, character in enumerate(text):
            if index % 8 == 0 and not keep_going():
                break
            if character == "\r":
                typed += 1
                continue
            # One event at a time, with pauses. Sent in one burst, modern applications (the
            # Windows 11 Notepad among them) can read the wrong character for a queued
            # Unicode key event: live testing showed text such as "tttthe 5555." typed as
            # "the quick ... 12345." Desktop.type_text also checks the result afterwards.
            shift = False
            if character == "\n":
                keys = [(_VK_RETURN, 0, False)]
            elif character == "\t":
                keys = [(_VK_TAB, 0, False)]
            elif (real := layout_key(character)) is not None:
                vk, scan, shift = real
                keys = [(vk, scan, False)]
                if shift:
                    _send([_key(_VK_SHIFT)])
                    time.sleep(_KEY_GAP_SECONDS)
            else:
                units = character.encode("utf-16-le")
                keys = [
                    (0, int.from_bytes(units[offset : offset + 2], "little"), True)
                    for offset in range(0, len(units), 2)
                ]
            for vk, scan, unicode in keys:
                _send([_key(vk, scan=scan, unicode=unicode)])
                time.sleep(_KEY_GAP_SECONDS)
                _send([_key(vk, scan=scan, unicode=unicode, up=True)])
                time.sleep(_KEY_GAP_SECONDS)
            if shift:
                _send([_key(_VK_SHIFT, up=True)])
                time.sleep(_KEY_GAP_SECONDS)
            typed += 1
            time.sleep(_CHARACTER_GAP_SECONDS)
        return typed

    def press_keys(self, keys: list[int]) -> None:
        _send([_key(vk) for vk in keys] + [_key(vk, up=True) for vk in reversed(keys)])

    def approval_dialog_open(self) -> bool:
        return bool(_user32.FindWindowW(None, APPROVAL_DIALOG_TITLE))

    def input_lock(self) -> Callable[[], None] | None:
        if not self._mutex:
            return None
        result = _kernel32.WaitForSingleObject(self._mutex, 3000)
        if result not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            return None
        return lambda: _kernel32.ReleaseMutex(self._mutex)

    def cursor_position(self) -> tuple[int, int]:
        point = wintypes.POINT()
        if not _user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("the pointer position could not be read")
        return point.x, point.y

    def primary_screen(self) -> Rect:
        return Rect(0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1))

    # UI Automation, started on first use.

    def _automation(self) -> UIAutomation:
        with self._automation_lock:
            if self._uia is None:
                from universal_ai_tools.desktop.uia import UIAutomation

                self._uia = UIAutomation()
            return self._uia

    def inspect_window(self, window_id: int, limit: int) -> tuple[list[ElementInfo], bool]:
        return self._automation().inspect(window_id, limit)

    def element_state(self, handle: int) -> ElementInfo | None:
        return self._automation().state(handle)

    def element_action(self, handle: int, action: ElementAction, value: str | None) -> bool:
        return self._automation().act(handle, action, value)

    def forget_elements(self, handles: list[int]) -> None:
        if self._uia is not None:
            self._uia.forget(handles)

    def element_text(self, handle: int, limit: int) -> str:
        return self._automation().text(handle, limit)

    def element_table(self, handle: int, max_rows: int, max_columns: int) -> TableData:
        return self._automation().table(handle, max_rows, max_columns)

    def focused_text(self) -> str | None:
        try:
            return self._automation().focused_text(_MAX_CHECKED_TEXT)
        except OSError:
            return None

    def start_overlay(self, text: Callable[[], str | None], on_hotkey: Callable[[], None]) -> None:
        if self._overlay is None:
            from universal_ai_tools.desktop.overlay import Overlay

            self._overlay = Overlay(text, on_hotkey)

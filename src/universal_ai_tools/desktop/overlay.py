"""The on-screen banner and the stop hotkey (Windows), on one thread with its own message loop.

The banner is a thin strip at the top centre of the main screen that says Claude is
controlling the PC, which programs, for how long, and how to stop it. It shows while an input
grant is active (``grants.py``) and hides otherwise. It never takes the focus or the mouse:
it is a layered, click-through (``WS_EX_TRANSPARENT``), no-activate, topmost tool window, so
clicks and keys go to whatever is under it. It has no title, so ``list_windows`` does not list
it, and it belongs to this process, so the input tools refuse it as a target anyway.

The stop hotkey (``Ctrl+Alt+Esc``) is registered with ``RegisterHotKey``: Windows then sends
this thread a message when, and only when, that combination is pressed. Nothing else about
the keyboard is seen, unlike a keyboard hook. Only one process can hold a hotkey; if another
Universal AI Tools process holds it, registration is retried every few seconds, and the stop
it triggers is shared through the stop file anyway.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("the overlay needs Windows")

logger = logging.getLogger(__name__)

HOTKEY_ID = 0x5541
HOTKEY_TEXT = "Ctrl+Alt+Esc"
_MOD_ALT = 0x1
_MOD_CONTROL = 0x2
_MOD_NOREPEAT = 0x4000
_VK_ESCAPE = 0x1B

_WM_DESTROY = 0x0002
_WM_PAINT = 0x000F
_WM_CLOSE = 0x0010
_WM_TIMER = 0x0113
_WM_HOTKEY = 0x0312
_WS_POPUP = 0x80000000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
EX_STYLE = (
    _WS_EX_TOPMOST | _WS_EX_TRANSPARENT | _WS_EX_TOOLWINDOW | _WS_EX_LAYERED | _WS_EX_NOACTIVATE
)
_LWA_ALPHA = 0x2
_SW_HIDE = 0
_SW_SHOWNOACTIVATE = 4
_HWND_TOPMOST = ctypes.c_void_p(-1 % (1 << (8 * ctypes.sizeof(ctypes.c_void_p))))
_SWP_NOSIZE = 0x1
_SWP_NOMOVE = 0x2
_SWP_NOACTIVATE = 0x10
_SWP_SHOWWINDOW = 0x40
_DT_CENTER = 0x1
_DT_VCENTER = 0x4
_DT_SINGLELINE = 0x20
_DT_END_ELLIPSIS = 0x8000
_TRANSPARENT = 1
_TIMER_ID = 1
_HOTKEY_RETRY_SECONDS = 5.0

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _WndClass(ctypes.Structure):
    _fields_ = (
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    )


class _PaintStruct(ctypes.Structure):
    _fields_ = (
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    )


def _sig(function: object, argtypes: list[object], restype: object) -> None:
    function.argtypes = argtypes  # type: ignore[attr-defined,unused-ignore]
    function.restype = restype  # type: ignore[attr-defined,unused-ignore]


_sig(_kernel32.GetModuleHandleW, [wintypes.LPCWSTR], wintypes.HMODULE)
_sig(_user32.RegisterClassW, [ctypes.POINTER(_WndClass)], wintypes.ATOM)
_sig(
    _user32.CreateWindowExW,
    [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
     wintypes.HINSTANCE, wintypes.LPVOID],
    wintypes.HWND,
)  # fmt: skip
_sig(
    _user32.DefWindowProcW,
    [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
    _LRESULT,
)
_sig(
    _user32.GetMessageW,
    [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT],
    wintypes.BOOL,
)
_sig(_user32.TranslateMessage, [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL)
_sig(_user32.DispatchMessageW, [ctypes.POINTER(wintypes.MSG)], _LRESULT)
_sig(
    _user32.PostMessageW,
    [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
    wintypes.BOOL,
)
_sig(_user32.PostQuitMessage, [ctypes.c_int], None)
_sig(_user32.DestroyWindow, [wintypes.HWND], wintypes.BOOL)
_sig(
    _user32.SetTimer,
    [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID],
    ctypes.c_size_t,
)
_sig(_user32.KillTimer, [wintypes.HWND, ctypes.c_size_t], wintypes.BOOL)
_sig(
    _user32.RegisterHotKey,
    [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT],
    wintypes.BOOL,
)
_sig(_user32.UnregisterHotKey, [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
_sig(_user32.ShowWindow, [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
_sig(_user32.IsWindowVisible, [wintypes.HWND], wintypes.BOOL)
_sig(
    _user32.SetWindowPos,
    [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ],
    wintypes.BOOL,
)
_sig(
    _user32.SetLayeredWindowAttributes,
    [wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD],
    wintypes.BOOL,
)
_sig(_user32.InvalidateRect, [wintypes.HWND, wintypes.LPVOID, wintypes.BOOL], wintypes.BOOL)
_sig(_user32.BeginPaint, [wintypes.HWND, ctypes.POINTER(_PaintStruct)], wintypes.HDC)
_sig(_user32.EndPaint, [wintypes.HWND, ctypes.POINTER(_PaintStruct)], wintypes.BOOL)
_sig(_user32.GetClientRect, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL)
_sig(_user32.FillRect, [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH], ctypes.c_int)
_sig(
    _user32.DrawTextW,
    [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT],
    ctypes.c_int,
)
_sig(_user32.GetSystemMetrics, [ctypes.c_int], ctypes.c_int)
_sig(_gdi32.CreateSolidBrush, [wintypes.DWORD], wintypes.HBRUSH)
_sig(
    _gdi32.CreateFontW,
    [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR],
    wintypes.HFONT,
)
_sig(_gdi32.SelectObject, [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ)
_sig(_gdi32.DeleteObject, [wintypes.HGDIOBJ], wintypes.BOOL)
_sig(_gdi32.SetTextColor, [wintypes.HDC, wintypes.DWORD], wintypes.DWORD)
_sig(_gdi32.SetBkMode, [wintypes.HDC, ctypes.c_int], ctypes.c_int)


def _rgb(red: int, green: int, blue: int) -> int:
    return red | (green << 8) | (blue << 16)


class Overlay:
    """The banner window and the stop hotkey. ``text()`` is polled every ``interval`` seconds."""

    def __init__(
        self,
        text: Callable[[], str | None],
        on_hotkey: Callable[[], None],
        *,
        width: int = 760,
        height: int = 34,
        interval: float = 1.0,
    ) -> None:
        self._text_source = text
        self._on_hotkey = on_hotkey
        self._width = width
        self._height = height
        self._interval_ms = max(100, int(interval * 1000))
        self._text: str | None = None
        self._hwnd = 0
        self._hotkey = False
        self._next_hotkey_try = 0.0
        self._ready = threading.Event()
        self._proc = _WNDPROC(self._window_proc)  # kept alive for the window's life
        self._thread = threading.Thread(target=self._run, name="uat-overlay", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def hotkey_registered(self) -> bool:
        return self._hotkey

    @property
    def visible(self) -> bool:
        return bool(self._hwnd and _user32.IsWindowVisible(self._hwnd))

    def refresh(self) -> None:
        """Re-read the text now instead of at the next tick."""
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, _WM_TIMER, _TIMER_ID, 0)

    def close(self) -> None:
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, _WM_CLOSE, 0, 0)
        self._thread.join(5)

    def _run(self) -> None:
        try:
            instance = _kernel32.GetModuleHandleW(None)
            name = f"UniversalAIToolsBanner{id(self)}"
            _user32.RegisterClassW(
                ctypes.byref(
                    _WndClass(lpfnWndProc=self._proc, hInstance=instance, lpszClassName=name)
                )
            )
            screen_width = _user32.GetSystemMetrics(0)
            left = max(0, (screen_width - self._width) // 2)
            hwnd = _user32.CreateWindowExW(
                EX_STYLE, name, None, _WS_POPUP, left, 0, self._width, self._height,
                None, None, instance, None,
            )  # fmt: skip
            if not hwnd:
                raise OSError(f"cannot create the banner window ({ctypes.get_last_error()})")
            _user32.SetLayeredWindowAttributes(hwnd, 0, 235, _LWA_ALPHA)
            self._brush = _gdi32.CreateSolidBrush(_rgb(176, 58, 0))
            self._font = _gdi32.CreateFontW(-17, 0, 0, 0, 600, 0, 0, 0, 0, 0, 0, 5, 0, "Segoe UI")
            self._hwnd = int(hwnd)
            _user32.SetTimer(hwnd, _TIMER_ID, self._interval_ms, None)
            self._try_hotkey()
            self._tick()
        except Exception:
            logger.exception("the banner and stop hotkey could not be started")
            self._ready.set()
            return
        self._ready.set()
        message = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(message))
            _user32.DispatchMessageW(ctypes.byref(message))
        with contextlib.suppress(Exception):
            _gdi32.DeleteObject(self._brush)
            _gdi32.DeleteObject(self._font)

    def _try_hotkey(self) -> None:
        if self._hotkey or time.monotonic() < self._next_hotkey_try:
            return
        self._hotkey = bool(
            _user32.RegisterHotKey(
                self._hwnd, HOTKEY_ID, _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT, _VK_ESCAPE
            )
        )
        if not self._hotkey:
            self._next_hotkey_try = time.monotonic() + _HOTKEY_RETRY_SECONDS

    def _tick(self) -> None:
        self._try_hotkey()
        try:
            text = self._text_source()
        except Exception:
            logger.exception("the banner text could not be read")
            text = None
        if text != self._text:
            self._text = text
            _user32.InvalidateRect(self._hwnd, None, True)
        if text:
            _user32.ShowWindow(self._hwnd, _SW_SHOWNOACTIVATE)
            _user32.SetWindowPos(
                self._hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
            )  # fmt: skip
        else:
            _user32.ShowWindow(self._hwnd, _SW_HIDE)

    def _paint(self, hwnd: int) -> None:
        paint = _PaintStruct()
        hdc = _user32.BeginPaint(hwnd, ctypes.byref(paint))
        try:
            rect = wintypes.RECT()
            _user32.GetClientRect(hwnd, ctypes.byref(rect))
            _user32.FillRect(hdc, ctypes.byref(rect), self._brush)
            _gdi32.SetBkMode(hdc, _TRANSPARENT)
            _gdi32.SetTextColor(hdc, _rgb(255, 255, 255))
            previous = _gdi32.SelectObject(hdc, self._font)
            _user32.DrawTextW(
                hdc, self._text or "", -1, ctypes.byref(rect),
                _DT_CENTER | _DT_VCENTER | _DT_SINGLELINE | _DT_END_ELLIPSIS,
            )  # fmt: skip
            _gdi32.SelectObject(hdc, previous)
        finally:
            _user32.EndPaint(hwnd, ctypes.byref(paint))

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            if message == _WM_TIMER:
                self._tick()
                return 0
            if message == _WM_HOTKEY and wparam == HOTKEY_ID:
                self._on_hotkey()
                self._tick()
                return 0
            if message == _WM_PAINT:
                self._paint(hwnd)
                return 0
            if message == _WM_CLOSE:
                _user32.KillTimer(hwnd, _TIMER_ID)
                if self._hotkey:
                    _user32.UnregisterHotKey(hwnd, HOTKEY_ID)
                    self._hotkey = False
                _user32.DestroyWindow(hwnd)
                return 0
            if message == _WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception:
            logger.exception("banner message %#x failed", message)
            return 0
        return int(_user32.DefWindowProcW(hwnd, message, wparam, lparam))


__all__ = ["EX_STYLE", "HOTKEY_ID", "HOTKEY_TEXT", "Overlay"]

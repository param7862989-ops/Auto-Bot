"""Whether this process has administrator rights.

The server refuses to offer the terminal or input tools from an elevated process (see
``server.check_privileges``): a command or a keystroke would then carry administrator
rights that the user never granted to the AI. It never asks for elevation itself.
"""

from __future__ import annotations

import os
import sys


def is_elevated() -> bool:
    """True for an elevated Windows token, or for root elsewhere. Unknown counts as elevated."""
    if sys.platform != "win32":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    token = wintypes.HANDLE()
    token_query = 0x0008
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        return True
    try:
        elevation = wintypes.DWORD()
        returned = wintypes.DWORD()
        token_elevation_class = 20
        if not advapi32.GetTokenInformation(
            token,
            token_elevation_class,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned),
        ):
            return True
        return bool(elevation.value)
    finally:
        kernel32.CloseHandle(token)

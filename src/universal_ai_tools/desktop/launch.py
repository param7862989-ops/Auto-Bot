"""Starting a program the user listed (``launch_app``), and nothing else.

Everywhere else the server starts no process (``tests/test_no_subprocess.py``). This module
is the one exception for the desktop tools, and it is narrow:

* only a program named in ``[input] launchable`` (an executable file name such as
  ``notepad.exe``), and never a shell, script host, interpreter, installer, or
  administration tool (``REFUSED_PROGRAMS``), whatever the list says;
* found only where an ordinary program cannot put or change files: under the Windows
  directory or ``Program Files``, directly or through the "App Paths" registry keys. A
  program that only exists in the user's own folders cannot be launched, because anything
  running as the user, including an approved command, could have put it there;
* with no arguments, so a launch cannot carry a command line, a script, or a file to open;
* started by the Windows shell (``os.startfile``) in its own directory, as if from the Start
  menu, and never waited for: the server keeps no handle to it.

Every launch needs the user's approval (``always_ask``), whatever grants are active.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REFUSED_PROGRAMS = frozenset(
    {
        # Shells and terminals.
        "cmd.exe", "powershell.exe", "powershell_ise.exe", "pwsh.exe", "bash.exe", "sh.exe",
        "wsl.exe", "wslhost.exe", "wt.exe", "windowsterminal.exe", "conhost.exe",
        "openconsole.exe", "mintty.exe",
        # Script hosts, interpreters, and things that run code they are given.
        "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "regsvr32.exe",
        "python.exe", "pythonw.exe", "py.exe", "pyw.exe", "node.exe", "java.exe", "javaw.exe",
        "msbuild.exe", "installutil.exe", "regasm.exe", "regsvcs.exe", "forfiles.exe",
        "hh.exe", "control.exe", "msiexec.exe", "explorer.exe", "runas.exe", "certutil.exe",
        "bitsadmin.exe", "curl.exe", "ftp.exe", "winget.exe",
        # Administration and security.
        "regedit.exe", "reg.exe", "schtasks.exe", "sc.exe", "taskmgr.exe", "mmc.exe",
        "msconfig.exe", "gpedit.msc", "netsh.exe", "bcdedit.exe", "diskpart.exe", "format.com",
        "takeown.exe", "icacls.exe", "cacls.exe", "net.exe", "net1.exe", "wmic.exe",
        "systemsettings.exe", "useraccountcontrolsettings.exe", "consent.exe",
        # Claude, and editors where Claude Code runs.
        "claude.exe", "code.exe", "cursor.exe", "windsurf.exe",
    }
)  # fmt: skip
_NAME = re.compile(r"^[a-z0-9][a-z0-9 ._+-]{0,63}\.exe$")


class LaunchRefused(Exception):
    """The program may not be launched; the reason is the message."""


def refusal(name: str) -> str | None:
    """Why ``name`` may never be launched, whatever the configuration, or None."""
    if not _NAME.fullmatch(name):
        return f"'{name[:64]}' is not an executable file name such as notepad.exe"
    if name in REFUSED_PROGRAMS:
        return f"{name} can run commands or change the system, so it is never launched"
    return None


def trusted_roots() -> list[Path]:
    """Folders only an administrator can change: Windows, and both Program Files."""
    roots = []
    for variable in ("SYSTEMROOT", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value))
    return roots


def _within(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return any(resolved.is_relative_to(root.resolve()) for root in roots if root.exists())


def resolve(name: str, *, roots: list[Path] | None = None) -> Path:
    """The executable to start for ``name``.

    Raises:
        LaunchRefused: If the name is refused, or no trusted copy of it is found.
    """
    reason = refusal(name)
    if reason is not None:
        raise LaunchRefused(reason)
    roots = trusted_roots() if roots is None else roots
    candidates: list[Path] = []
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        candidates += [Path(system_root) / "System32" / name, Path(system_root) / name]
    candidates += _app_paths(name)
    for candidate in candidates:
        if candidate.is_file() and candidate.name.casefold() == name and _within(candidate, roots):
            return candidate.resolve()
    raise LaunchRefused(
        f"{name} was not found under the Windows folder or Program Files (programs installed "
        "only for one user cannot be launched)"
    )


def _app_paths(name: str) -> list[Path]:
    """Where the "App Paths" registry keys say ``name`` is installed."""
    if sys.platform != "win32":
        return []
    import winreg

    found = []
    key = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{name}"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, "")
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            found.append(Path(os.path.expandvars(value.strip().strip('"'))))
    return found


def start(path: Path) -> None:
    """Start ``path`` with no arguments, in its own folder, without waiting for it.

    Raises:
        OSError: If Windows could not start it.
    """
    if sys.platform != "win32":
        raise OSError("launching programs is available on Windows only")
    os.startfile(str(path), "open", "", str(path.parent))  # noqa: S606 - checked path, no arguments


__all__ = ["REFUSED_PROGRAMS", "LaunchRefused", "refusal", "resolve", "start", "trusted_roots"]

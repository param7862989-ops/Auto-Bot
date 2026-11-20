"""What the server must never change: its own code, the Python running it, its configuration.

The file tools may change files in the workspace, and the workspace may be any project,
including a checkout of this repository. If the server could rewrite the code that enforces
its permissions (``gate.py``, ``approval.py``, ``policy.py``, ``workspace.py``, ...), the
Python environment that runs it, or a directory Python imports from, then one approved
edit could switch approval off for every later session. So ``Workspace`` refuses to create,
change, or move anything at or below a protected location, and refuses to move a directory
that contains one.

The protected locations are found from the running process, not from file names, and are
kept up to date while it runs (``_Runtime.refresh``, called before every check):

* the directory the ``universal_ai_tools`` package was imported from;
* the location of every top-level module imported so far (``mcp``, ``pydantic``, the
  standard library, ...), including modules imported after startup: what the server runs is
  the code it has loaded;
* every directory on ``sys.path``, including entries added after startup, the one an
  editable install's ``.pth`` file adds (a checkout's ``src``), and the current directory
  when Python put it there;
* every directory named by a ``.pth`` file in a directory on ``sys.path`` or a site
  directory, whether or not it exists yet: Python adds it to ``sys.path`` on the next start;
* the package directories that a setuptools editable finder (``__editable___*_finder``)
  maps module names to, which are on no path at all;
* the per-user site directories, which Python would add on a later start even if they do
  not exist yet;
* ``sys.prefix``, ``sys.base_prefix`` (and the exec prefixes), and the interpreter's
  directory: the virtual environment, its ``pyvenv.cfg``, and the base installation;
* places from which Windows runs things by itself (``autostart_paths``): the per-user and
  all-users Startup folders, and the folders holding PowerShell profiles. A file put there
  would run a program later, which is command execution by another route;
* in file mode, the config file, its directory, the audit log and its rotation and lock
  files, and Claude's own settings (``claude_configuration_paths``).

Every location is reduced to its real path (links and junctions resolved, Windows 8.3
short names expanded) and compared case-insensitively on Windows, and ``Workspace`` compares
it with the real path of what it actually opened, so an alias, a junction, or a different
spelling of the same directory is caught.

A directory on ``sys.path`` or a Python prefix (``/usr`` for a system Python on Linux) that
strictly contains the workspace does not make the whole workspace protected: Python uses
only the entries directly inside such a directory, and none of those is inside the
workspace. A protected location of any other kind that contains the workspace (the
workspace is inside the server's own code, or inside Claude's settings) protects all of it.

What this does not cover: a module imported for the first time from a new location is
protected from the moment it is imported, not before. A location that only a future version
of the code, or a future ``.pth`` file outside every known site directory, would import from
cannot be known now. Programs other than this server, and Claude Code's own file tools, are
not limited by it. Input tools could type into an editor that has one open; editors are
among the protected windows.
"""

from __future__ import annotations

import contextlib
import os
import site
import sys
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from universal_ai_tools.settings import ServiceConfig

SERVER_CODE = "the Universal AI Tools server code"
LOADED_CODE = "code the running server has loaded"
IMPORT_PATH = "a directory the server imports code from"
PYTHON_ENVIRONMENT = "the Python environment that runs the server"
SERVER_CONFIGURATION = "the server's configuration or audit log"
CLIENT_SETTINGS = "Claude's own settings"
AUTOSTART = "a place from which Windows or PowerShell runs programs by itself"

MAX_PTH_BYTES = 64 * 1024


def normalize(path: str | os.PathLike[str]) -> str:
    """The form protected locations are compared in: real, absolute, normalized, case-folded."""
    text = os.fspath(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[len("\\\\?\\UNC\\") :]
    text = text.removeprefix("\\\\?\\")
    return os.path.normcase(os.path.normpath(text))


def _real(path: str | os.PathLike[str]) -> str:
    try:
        return normalize(os.path.realpath(path))
    except (OSError, ValueError):
        return normalize(os.path.abspath(path))


def real_path(path: str | os.PathLike[str]) -> str:
    """``path`` as protected locations are compared: links resolved, then ``normalize``d."""
    return _real(path)


def is_within(candidate: str, root: str) -> bool:
    """Whether normalized ``candidate`` is normalized ``root`` or below it."""
    return _is_within(candidate, root)


def _is_within(candidate: str, root: str) -> bool:
    """Whether ``candidate`` is ``root`` or below it. Both already normalized."""
    if candidate == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return candidate.startswith(prefix)


@dataclass(frozen=True)
class ProtectedPath:
    path: str
    """Normalized real path."""
    reason: str
    above_exempt: bool = False
    """When it strictly contains the workspace, it protects nothing in the workspace.

    True for directories Python imports from and for Python's own prefixes, where only
    entries directly inside them matter and those are protected on their own."""


_Found = dict[str, ProtectedPath]


def _add(
    found: _Found, path: str | os.PathLike[str], reason: str, *, above_exempt: bool = False
) -> bool:
    """Add one location. True if it was new or became stricter."""
    text = os.fspath(path)
    if not text:
        return False
    real = _real(text)
    known = found.get(real)
    if known is not None:
        # Keep the stricter meaning: a location that is not only an import root.
        if known.above_exempt and not above_exempt:
            found[real] = ProtectedPath(real, reason)
            return True
        return False
    found[real] = ProtectedPath(real, reason, above_exempt)
    return True


class TrustedPaths:
    """A set of protected locations, checked against real paths.

    Some are fixed when the set is made; a set made by ``for_runtime`` (or combined with one)
    also follows the running process: see ``_Runtime``.
    """

    def __init__(
        self,
        entries: Iterable[
            tuple[str | os.PathLike[str], str] | tuple[str | os.PathLike[str], str, bool]
        ] = (),
    ) -> None:
        """``entries`` are ``(path, reason)`` or ``(path, reason, above_exempt)``."""
        found: _Found = {}
        for entry in entries:
            path, reason, *flag = entry
            _add(found, path, reason, above_exempt=bool(flag and flag[0]))
        self._entries = tuple(found.values())
        self._runtime: _Runtime | None = None

    def _all(self) -> tuple[ProtectedPath, ...]:
        if self._runtime is None:
            return self._entries
        return self._entries + self._runtime.refresh()

    def __iter__(self) -> Iterator[ProtectedPath]:
        return iter(self._all())

    def __len__(self) -> int:
        return len(self._all())

    def combined(self, other: TrustedPaths) -> TrustedPaths:
        merged = TrustedPaths()
        found: _Found = {}
        for entry in (*self._entries, *other._entries):
            known = found.get(entry.path)
            if known is None or (known.above_exempt and not entry.above_exempt):
                found[entry.path] = entry
        merged._entries = tuple(found.values())
        merged._runtime = self._runtime or other._runtime
        return merged

    def protecting(self, real: str, workspace_root: str) -> ProtectedPath | None:
        """The protected location that ``real`` is at or below, if any.

        Args:
            real: The real path of what would be created or changed.
            workspace_root: The real path of the workspace root.
        """
        candidate = normalize(real)
        root = normalize(workspace_root)
        for entry in self._all():
            if not _is_within(candidate, entry.path):
                continue
            if entry.above_exempt and entry.path != root and _is_within(root, entry.path):
                # A sys.path entry or a prefix above the workspace: Python uses only its
                # direct entries, none of which is inside the workspace.
                continue
            return entry
        return None

    def contained_in(self, real: str) -> ProtectedPath | None:
        """A protected location strictly below ``real``, which moving ``real`` would move."""
        candidate = normalize(real)
        for entry in self._all():
            if entry.path != candidate and _is_within(entry.path, candidate):
                return entry
        return None

    @classmethod
    def for_runtime(cls) -> TrustedPaths:
        """The code and Python environment of this process, followed while it runs."""
        trusted = cls()
        trusted._runtime = _Runtime()
        return trusted

    @classmethod
    def for_service(
        cls,
        service: ServiceConfig,
        *,
        runtime: TrustedPaths | None = None,
        home: Path | None = None,
    ) -> TrustedPaths:
        """``runtime`` (by default ``for_runtime()``), plus, in file mode, the configuration
        file and its directory, the audit log, and Claude's settings."""
        runtime = runtime if runtime is not None else cls.for_runtime()
        if not service.file_mode:
            return runtime
        return cls.for_state(service.config_path, service.audit_path, runtime=runtime, home=home)

    @classmethod
    def for_state(
        cls,
        config_path: Path | None,
        audit_path: Path | None,
        *,
        runtime: TrustedPaths | None = None,
        home: Path | None = None,
    ) -> TrustedPaths:
        """``runtime`` (by default ``for_runtime()``), plus the configuration file and its
        directory, the audit log and its rotation and lock files, and Claude's settings.

        What the file-mode server and the Claude Code guard (``universal_ai_tools.guard``)
        both protect; the guard has no ``ServiceConfig``, which needs pydantic to build."""
        runtime = runtime if runtime is not None else cls.for_runtime()
        entries: list[tuple[str | os.PathLike[str], str]] = []
        if config_path is not None:
            entries += [
                (config_path, SERVER_CONFIGURATION),
                (config_path.parent, SERVER_CONFIGURATION),
            ]
        if audit_path is not None:
            entries += [
                (audit_path, SERVER_CONFIGURATION),
                (audit_path.with_name(audit_path.name + ".1"), SERVER_CONFIGURATION),
                (audit_path.with_name(audit_path.name + ".lock"), SERVER_CONFIGURATION),
            ]
        entries += [(path, CLIENT_SETTINGS) for path in claude_configuration_paths(home)]
        return runtime.combined(cls(entries))


class _Runtime:
    """The locations of this process's code, found once and then followed as it changes.

    ``refresh`` looks only at what is new since the last call: modules not seen before in
    ``sys.modules``, and a changed ``sys.path`` (whose new entries' ``.pth`` files are read).
    It is cheap enough to run before every check, which is what closes the gap between a
    late import and the next change.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._found: _Found = {}
        self._modules: set[str] = set()
        self._path: tuple[str, ...] = ()
        self._scanned: set[str] = set()
        self._entries: tuple[ProtectedPath, ...] = ()
        self._dirty = True
        import universal_ai_tools

        found = self._found
        _add(found, Path(universal_ai_tools.__file__).parent, SERVER_CODE)
        try:
            user_site = site.getusersitepackages()
            user_base = site.getuserbase()
        except (AttributeError, OSError):
            user_site = user_base = ""
        _add(found, user_site, IMPORT_PATH, above_exempt=True)
        _add(found, user_base, PYTHON_ENVIRONMENT, above_exempt=True)
        for prefix in {sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix}:
            _add(found, prefix, PYTHON_ENVIRONMENT, above_exempt=True)
        for executable in (sys.executable, getattr(sys, "_base_executable", "")):
            if executable:
                _add(found, Path(executable).parent, PYTHON_ENVIRONMENT, above_exempt=True)
        for path in autostart_paths():
            _add(found, path, AUTOSTART)
        with contextlib.suppress(AttributeError, OSError):
            for directory in (*site.getsitepackages(), user_site):
                self._read_pth_files(directory)
        self.refresh()

    def refresh(self) -> tuple[ProtectedPath, ...]:
        with self._lock:
            found = self._found
            for name in [name for name in list(sys.modules) if name not in self._modules]:
                self._modules.add(name)
                module = sys.modules.get(name)
                if module is None:
                    continue
                if "." not in name:
                    for location in _module_locations(module):
                        self._dirty |= _add(found, location, LOADED_CODE)
                for location in _editable_locations(name, module):
                    self._dirty |= _add(found, location, LOADED_CODE)
            path = tuple(entry for entry in list(sys.path) if isinstance(entry, str))
            if path != self._path:
                self._path = path
                for entry in path:
                    directory = entry or os.getcwd()
                    self._dirty |= _add(found, directory, IMPORT_PATH, above_exempt=True)
                    self._read_pth_files(directory)
            if self._dirty:
                self._entries = tuple(found.values())
                self._dirty = False
            return self._entries

    def _read_pth_files(self, directory: str) -> None:
        """Protect every directory a ``.pth`` file in ``directory`` names (see ``site``)."""
        if not directory or directory in self._scanned:
            return
        self._scanned.add(directory)
        try:
            with os.scandir(directory) as entries:
                names = [entry.name for entry in entries if entry.name.lower().endswith(".pth")]
        except OSError:
            return
        for name in names:
            try:
                with open(os.path.join(directory, name), "rb") as handle:
                    text = handle.read(MAX_PTH_BYTES).decode("utf-8", "replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.rstrip()
                # site.py: comments are skipped; lines starting with "import" run code (the
                # .pth file itself is in a protected directory); anything else is a path.
                if not line or line.startswith("#") or line.startswith(("import ", "import\t")):
                    continue
                self._dirty |= _add(
                    self._found, os.path.join(directory, line), IMPORT_PATH, above_exempt=True
                )


def _module_locations(module: object) -> list[str]:
    """Where a top-level module's code lives: its file, or its package directories."""
    locations: list[str] = []
    path = getattr(module, "__path__", None)
    if path is not None:
        with contextlib.suppress(TypeError):
            locations += [entry for entry in path if isinstance(entry, str)]
    else:
        file = getattr(module, "__file__", None)
        if isinstance(file, str):
            locations.append(file)
    return locations


def _editable_locations(name: str, module: object) -> list[str]:
    """The source directories a setuptools editable finder maps package names to."""
    if not name.startswith("__editable__"):
        return []
    locations: list[str] = []
    mapping = getattr(module, "MAPPING", None)
    if isinstance(mapping, dict):
        locations += [value for value in mapping.values() if isinstance(value, str)]
    namespaces = getattr(module, "NAMESPACES", None)
    if isinstance(namespaces, dict):
        for values in namespaces.values():
            if isinstance(values, list | tuple):
                locations += [value for value in values if isinstance(value, str)]
    return locations


def autostart_paths() -> list[Path]:
    """Folders from which Windows or PowerShell starts programs without being asked."""
    if sys.platform != "win32":
        home = Path.home()
        return [home / ".config" / "autostart"]
    found: list[Path] = []
    startup = _known_folder("{B97D20BB-F46A-4C97-BA10-5E3608430854}")
    common_startup = _known_folder("{82A5EA35-D9CD-47C5-9629-E15D2F714E6E}")
    documents = _known_folder("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("PROGRAMDATA")
    startup_suffix = Path("Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    if startup is None and appdata:
        startup = Path(appdata) / startup_suffix
    if common_startup is None and programdata:
        common_startup = Path(programdata) / startup_suffix
    if documents is None:
        documents = Path.home() / "Documents"
    for path in (startup, common_startup):
        if path is not None:
            found.append(path)
    found += [documents / "WindowsPowerShell", documents / "PowerShell"]
    return found


if sys.platform == "win32":

    def _known_folder(guid: str) -> Path | None:
        """A Windows known folder's path, or None if it cannot be read."""
        import ctypes
        import uuid
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = (
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            )

        folder = _GUID.from_buffer_copy(uuid.UUID(guid).bytes_le)
        pointer = ctypes.c_wchar_p()
        try:
            shell32 = ctypes.WinDLL("shell32")
            ole32 = ctypes.WinDLL("ole32")
            result = shell32.SHGetKnownFolderPath(
                ctypes.byref(folder), 0, None, ctypes.byref(pointer)
            )
        except (AttributeError, OSError):
            return None
        try:
            if result != 0 or not pointer.value:
                return None
            return Path(pointer.value)
        finally:
            ole32.CoTaskMemFree(pointer)

else:

    def _known_folder(guid: str) -> Path | None:
        return None


def claude_configuration_paths(home: Path | None = None) -> list[Path]:
    """Where Claude Desktop and Claude Code keep their settings, existing or not."""
    home = home or Path.home()
    paths = [home / ".claude", home / ".claude.json"]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        local = os.environ.get("LOCALAPPDATA")
        if appdata:
            paths.append(Path(appdata) / "Claude")
        if local:
            packages = Path(local) / "Packages"
            with contextlib.suppress(OSError):
                paths += sorted(packages.glob("Claude_*"))
    elif sys.platform == "darwin":
        paths.append(home / "Library" / "Application Support" / "Claude")
    else:
        paths.append(home / ".config" / "Claude")
    return paths


def import_path_contains(directory: Path) -> bool:
    """Whether ``directory`` (or the current directory standing for it) is on ``sys.path``."""
    target = _real(directory)
    for entry in sys.path:
        if isinstance(entry, str) and _real(entry or os.getcwd()) == target:
            return True
    return False


def current_directory_on_import_path() -> bool:
    """Whether Python imports from the directory this process was started in.

    True when it was started without ``-I`` or ``-P`` with ``-m`` or ``-c``: then a file such
    as ``json.py`` in that directory replaces a module, before any code here can object.
    """
    return import_path_contains(Path(os.getcwd()))


__all__ = [
    "AUTOSTART",
    "ProtectedPath",
    "TrustedPaths",
    "autostart_paths",
    "claude_configuration_paths",
    "current_directory_on_import_path",
    "import_path_contains",
    "is_within",
    "normalize",
    "real_path",
]

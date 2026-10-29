"""Workspace path confinement: the security boundary for filesystem tools.

Every path a tool receives is untrusted. ``Workspace`` turns such a path into a
verified location inside the authorized workspace root, or refuses it with a
``WorkspaceError``. Error messages are safe to show to the client: they name only
the workspace-relative path and never include file contents or absolute paths.

Checks, in order:

1. Lexical, before touching the filesystem: the path must be relative and must not
   contain ``..``, NUL, or a sensitive directory name. On Windows, drive letters,
   UNC prefixes, alternate data streams, and reserved device names are refused.
2. Per component, with ``os.lstat``: every component must be a real directory or
   file, not a symlink, Windows junction, or other name-surrogate reparse point.
3. After opening a file: the descriptor must refer to a regular file, must be the
   same file (device and inode) that step 2 inspected, and, where the platform can
   report it, its real path must lie inside the workspace root.

Step 3 means file contents from outside the root are never returned, even if a
directory is swapped for a link between the check and the open. Directory
listings rely on steps 1-2 plus a re-check of the directory's real path, which
narrows but does not close that window. See docs/architecture.md for the
remaining limitations.

``create_file`` is the only operation that changes the filesystem. It creates one
new file with an exclusive create (``O_CREAT | O_EXCL`` or ``CREATE_NEW``), so it
never replaces anything, and it removes only a file it created itself, identified
by its open handle or inode. Nothing here renames, overwrites, or deletes
existing files.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

# Directories whose contents no tool may access, at any depth.
SENSITIVE_DIRECTORY_NAMES = frozenset({".ssh", ".gnupg", ".aws"})

# Files that read_file refuses and search_files skips. Names are matched
# case-insensitively. Listing them (names only) is allowed.
_SENSITIVE_FILE_NAMES = frozenset(
    {".env", ".netrc", ".pgpass", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
)
_SENSITIVE_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_REPARSE_TAG_NAME_SURROGATE = 0x20000000

_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOINHERIT", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    # Never block on a FIFO or claim a terminal, should one slip past the type checks.
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
)


class WorkspaceError(Exception):
    """A refused or failed filesystem request. The message is safe to show to clients."""


class FileTooLargeError(WorkspaceError):
    """A file exceeds the size limit for the requested operation."""


class PathNotFoundError(WorkspaceError):
    """A path, or one of its parent directories, does not exist."""


def is_sensitive_file_name(name: str) -> bool:
    """Return whether ``name`` looks like a credential or secrets file."""
    folded = name.casefold()
    if folded in _ENV_TEMPLATE_NAMES:
        return False
    return (
        folded in _SENSITIVE_FILE_NAMES
        or folded.startswith(".env.")
        or folded.endswith(_SENSITIVE_FILE_SUFFIXES)
    )


def is_link(st: os.stat_result) -> bool:
    """Return whether an ``lstat`` result is a symlink, junction, or similar redirect."""
    if stat.S_ISLNK(st.st_mode):
        return True
    if sys.platform == "win32":
        # Junctions and other name surrogates redirect to another path. Other reparse
        # points, such as OneDrive cloud files, are ordinary files and are allowed.
        return bool(
            st.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            and st.st_reparse_tag & _REPARSE_TAG_NAME_SURROGATE
        )
    return False


@dataclass(frozen=True)
class ResolvedPath:
    """A location inside the workspace that passed the lexical and ``lstat`` checks."""

    parts: tuple[str, ...]
    """Components relative to the workspace root; empty for the root itself."""

    absolute: Path
    stat: os.stat_result
    """``lstat`` result captured when the path was checked."""

    @property
    def relative(self) -> str:
        """Workspace-relative path with ``/`` separators; ``.`` for the root."""
        return "/".join(self.parts) or "."

    @property
    def name(self) -> str:
        return self.parts[-1] if self.parts else "."

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.stat.st_mode)

    @property
    def is_file(self) -> bool:
        return stat.S_ISREG(self.stat.st_mode)

    @property
    def is_link(self) -> bool:
        return is_link(self.stat)


class Workspace:
    """Confines filesystem access to one authorized root directory.

    The root must already be absolute and fully resolved, as ``AppConfig``
    guarantees. It is never widened.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """Resolved root directory this workspace is confined to."""
        return self._root

    def resolve(self, raw: str, *, for_reading: bool = False) -> ResolvedPath:
        """Validate an untrusted relative path and ``lstat`` each of its components.

        Args:
            raw: Path relative to the workspace root. ``/`` and ``\\`` are both
                separators; ``""`` and ``"."`` mean the root.
            for_reading: Also refuse a sensitive final file name before the
                filesystem is touched, so refusal does not reveal whether it exists.

        Raises:
            WorkspaceError: If the path is malformed, escapes the workspace, passes
                through a link, or does not exist.
        """
        parts = _split_relative(raw)
        if for_reading and parts and is_sensitive_file_name(parts[-1]):
            raise WorkspaceError(f"reading sensitive files is not allowed: '{'/'.join(parts)}'")

        relative = "/".join(parts) or "."
        current = self._root
        st = self._lstat(current, relative)
        for index, part in enumerate(parts):
            current = current / part
            st = self._lstat(current, relative)
            prefix = "/".join(parts[: index + 1])
            if is_link(st):
                raise WorkspaceError(f"refusing to follow symlink or junction: '{prefix}'")
            if index < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
                raise WorkspaceError(f"not a directory: '{prefix}'")
        return ResolvedPath(tuple(parts), current, st)

    def scan_directory(self, directory: ResolvedPath) -> list[ResolvedPath]:
        """Return the entries of ``directory``, sorted by name, without following links.

        Entries that vanish while being listed are omitted.
        """
        if not directory.is_dir:
            raise WorkspaceError(f"not a directory: '{directory.relative}'")
        try:
            real = os.path.realpath(directory.absolute, strict=True)
            self._require_inside(real, directory.relative, is_file=False)
            with os.scandir(directory.absolute) as iterator:
                found = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise _os_error(exc, directory.relative) from None

        children: list[ResolvedPath] = []
        for entry in found:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            children.append(
                ResolvedPath((*directory.parts, entry.name), directory.absolute / entry.name, st)
            )
        return children

    def read_bytes(self, target: ResolvedPath, max_bytes: int) -> bytes:
        """Read a regular file of at most ``max_bytes`` bytes after verifying what was opened.

        Raises:
            FileTooLargeError: If the file is larger than ``max_bytes``.
            WorkspaceError: If the target is not a regular file inside the workspace,
                changed while being opened, or cannot be read.
        """
        relative = target.relative
        try:
            before = os.lstat(target.absolute)
        except OSError as exc:
            raise _os_error(exc, relative) from None
        if is_link(before):
            raise WorkspaceError(f"refusing to follow symlink or junction: '{relative}'")
        if stat.S_ISDIR(before.st_mode):
            raise WorkspaceError(f"is a directory, not a file: '{relative}'")
        if not stat.S_ISREG(before.st_mode):
            raise WorkspaceError(f"not a regular file: '{relative}'")

        try:
            fd = os.open(target.absolute, _OPEN_FLAGS)
        except OSError as exc:
            raise _os_error(exc, relative) from None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
                raise WorkspaceError(f"file changed while being opened: '{relative}'")
            real = _real_path_of_fd(fd)
            if real is not None:
                self._require_inside(real, relative, is_file=True)
            if opened.st_size > max_bytes:
                raise FileTooLargeError(
                    f"file is too large: '{relative}' is {opened.st_size} bytes; "
                    f"the limit is {max_bytes} bytes"
                )
            data = _read_up_to(fd, max_bytes + 1)
        except OSError as exc:
            raise _os_error(exc, relative) from None
        finally:
            os.close(fd)
        if len(data) > max_bytes:
            raise FileTooLargeError(
                f"file is too large: '{relative}' exceeds the limit of {max_bytes} bytes"
            )
        return data

    def create_file(
        self, raw: str, data: bytes, *, is_protected_directory: Callable[[str], bool]
    ) -> str:
        """Create a new file containing ``data``. Never replaces anything that exists.

        Args:
            raw: Untrusted path of the new file, relative to the workspace root. Its
                parent directory must already exist.
            data: The complete file contents.
            is_protected_directory: Returns whether a path component names a directory
                that must not be written into (such as ``.git``). It is applied to the
                requested path and to the parent directory's real path.

        Returns:
            The workspace-relative path of the created file.

        Raises:
            WorkspaceError: If the path is refused, the parent is missing or not a real
                directory, anything already exists at the path, or the write fails. A
                file this call created is removed again before a write failure is
                reported, unless the error message says it could not be.
        """
        parts = _split_relative(raw)
        if not parts or raw.replace("\\", "/").rsplit("/", 1)[-1] in ("", "."):
            raise WorkspaceError("path must name a file, not a directory")
        relative = "/".join(parts)
        if is_sensitive_file_name(parts[-1]):
            raise WorkspaceError(f"writing sensitive files is not allowed: '{relative}'")
        if any(is_protected_directory(part) for part in parts):
            raise WorkspaceError(f"writing to excluded directories is not allowed: '{relative}'")

        parent_relative = "/".join(parts[:-1]) or "."
        try:
            parent = self.resolve(parent_relative)
        except PathNotFoundError:
            raise WorkspaceError(f"parent directory does not exist: '{parent_relative}'") from None
        if not parent.is_dir:
            raise WorkspaceError(f"not a directory: '{parent_relative}'")
        try:
            real_parent = os.path.realpath(parent.absolute, strict=True)
        except OSError as exc:
            raise _os_error(exc, parent_relative) from None
        # The real path also catches aliases such as the 8.3 short name GIT~1 for .git.
        inside = self._require_inside(real_parent, relative, is_file=False)
        if any(is_protected_directory(part) for part in inside):
            raise WorkspaceError(f"writing to excluded directories is not allowed: '{relative}'")

        name = parts[-1]
        target = parent.absolute / name
        if self._exists(target, relative):
            raise WorkspaceError(f"already exists: '{relative}'")
        try:
            new = _create_new_file(self._root, parent, name)
        except OSError as exc:
            # Something appeared since the check. Windows reports an existing directory
            # or junction as access denied rather than as already existing.
            if isinstance(exc, FileExistsError) or self._exists(target, relative):
                raise WorkspaceError(f"already exists: '{relative}'") from None
            raise _os_error(exc, relative) from None

        try:
            self._verify_created(new.fd, real_parent, name, relative, is_protected_directory)
            _write_all(new.fd, data)
            os.fsync(new.fd)
            new.close_file()
        except BaseException as exc:
            removed = new.discard()
            new.release()
            if not isinstance(exc, OSError | WorkspaceError):
                raise
            reason = (
                str(exc) if isinstance(exc, WorkspaceError) else f"failed to write '{relative}'"
            )
            outcome = (
                "the new file was removed"
                if removed
                else "the incomplete file could not be removed safely"
            )
            raise WorkspaceError(f"{reason}; {outcome}") from None
        new.release()
        return relative

    def _verify_created(
        self,
        fd: int,
        real_parent: str,
        name: str,
        relative: str,
        is_protected_directory: Callable[[str], bool],
    ) -> None:
        """Check, before writing, that the new file landed where it was meant to."""
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkspaceError(f"file changed while being created: '{relative}'")
        real = _real_path_of_fd(fd)
        if real is None:
            # Only possible off Windows, where the directory walk in _create_new_file
            # already pinned the parent directory.
            return
        inside = self._require_inside(real, relative, is_file=False)
        if any(is_protected_directory(part) for part in inside):
            raise WorkspaceError(f"writing to excluded directories is not allowed: '{relative}'")
        # Windows creates by path, so a parent swapped for a junction after the checks
        # would redirect the file; refuse unless it is exactly where it was checked.
        if sys.platform == "win32" and Path(real) != Path(real_parent) / name:
            raise WorkspaceError(
                f"directory changed while the file was being created: '{relative}'"
            )

    def _exists(self, path: Path, relative: str) -> bool:
        """Return whether anything, including a link, exists at ``path``."""
        try:
            os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _os_error(exc, relative) from None
        return True

    def _lstat(self, path: Path, relative: str) -> os.stat_result:
        try:
            return os.lstat(path)
        except OSError as exc:
            raise _os_error(exc, relative) from None

    def _require_inside(self, real: str, relative: str, *, is_file: bool) -> tuple[str, ...]:
        """Refuse a real (fully resolved) path outside the root or in a sensitive location.

        Checking the real path also catches aliases that lexical checks cannot,
        such as Windows 8.3 short names. Returns the real path's components
        relative to the root.
        """
        try:
            inside = Path(real).relative_to(self._root).parts
        except ValueError:
            raise WorkspaceError(f"path resolves outside the workspace: '{relative}'") from None
        if any(part.casefold() in SENSITIVE_DIRECTORY_NAMES for part in inside):
            raise WorkspaceError(f"access to sensitive directories is not allowed: '{relative}'")
        if is_file and inside and is_sensitive_file_name(inside[-1]):
            raise WorkspaceError(f"reading sensitive files is not allowed: '{relative}'")
        return inside


def _split_relative(raw: str) -> list[str]:
    if "\x00" in raw:
        raise WorkspaceError("path must not contain NUL characters")
    if (
        raw.startswith(("/", "\\"))
        or PurePosixPath(raw).is_absolute()
        or PureWindowsPath(raw).drive
    ):
        raise WorkspaceError(
            "path must be relative to the workspace root; absolute paths, drive letters, "
            "and UNC paths are not allowed"
        )
    parts = [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]
    if ".." in parts:
        raise WorkspaceError("path must not contain '..' components")
    if sys.platform == "win32":
        for part in parts:
            if ":" in part or os.path.isreserved(part):
                raise WorkspaceError(f"path component is not allowed on Windows: '{part}'")
    if any(part.casefold() in SENSITIVE_DIRECTORY_NAMES for part in parts):
        raise WorkspaceError(f"access to sensitive directories is not allowed: '{'/'.join(parts)}'")
    return parts


def _os_error(exc: OSError, relative: str) -> WorkspaceError:
    # Never include exc.filename or str(exc): both can contain absolute paths.
    if isinstance(exc, FileNotFoundError):
        return PathNotFoundError(f"not found: '{relative}'")
    if isinstance(exc, NotADirectoryError):
        return WorkspaceError(f"not a directory: '{relative}'")
    if isinstance(exc, PermissionError):
        return WorkspaceError(f"permission denied: '{relative}'")
    return WorkspaceError(f"cannot access '{relative}'")


def _read_up_to(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1 << 16))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "write made no progress")
        view = view[written:]


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _get_final_path_name = _kernel32.GetFinalPathNameByHandleW
    _get_final_path_name.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _get_final_path_name.restype = wintypes.DWORD

    def _real_path_of_fd(fd: int) -> str | None:
        """Return the real path of an open file, as the OS reports it."""
        handle = msvcrt.get_osfhandle(fd)
        size = 512
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            length = _get_final_path_name(handle, buffer, size, 0)
            if length == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            if length < size:
                break
            size = length + 1
        path = buffer.value
        if path.startswith("\\\\?\\UNC\\"):
            return "\\\\" + path[len("\\\\?\\UNC\\") :]
        return path.removeprefix("\\\\?\\")

elif sys.platform == "linux":

    def _real_path_of_fd(fd: int) -> str | None:
        """Return the real path of an open file, or None if /proc is unavailable."""
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return None

elif sys.platform == "darwin":
    import fcntl

    def _real_path_of_fd(fd: int) -> str | None:
        """Return the real path of an open file, as the OS reports it."""
        result = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        return result.rstrip(b"\x00").decode(sys.getfilesystemencoding())

else:

    def _real_path_of_fd(fd: int) -> str | None:
        """No portable way to ask; rely on the inode identity check instead."""
        return None


# Exclusive creation. _create_new_file returns a _NewFile whose ``fd`` is open for
# writing. ``discard`` removes the file only if it is still the one this process
# created; ``release`` closes everything that is still open.

if sys.platform == "win32":
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_READ_ATTRIBUTES = 0x0080
    _CREATE_NEW = 1
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_DISPOSITION_INFO_CLASS = 4
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    _create_file_w = _kernel32.CreateFileW
    _create_file_w.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _create_file_w.restype = wintypes.HANDLE
    _set_file_information = _kernel32.SetFileInformationByHandle
    _set_file_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _set_file_information.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL

    def _mark_for_deletion(handle: int) -> bool:
        """Delete the file behind ``handle`` when it is closed, whatever its name is by then."""
        delete = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile
        return bool(
            _set_file_information(
                handle, _FILE_DISPOSITION_INFO_CLASS, ctypes.byref(delete), ctypes.sizeof(delete)
            )
        )

    class _NewFile:
        def __init__(self, fd: int) -> None:
            self.fd = fd
            self._open = True

        def close_file(self) -> None:
            self._open = False
            os.close(self.fd)

        def discard(self) -> bool:
            # Deleting through the handle can only ever remove the file we created.
            return self._open and _mark_for_deletion(msvcrt.get_osfhandle(self.fd))

        def release(self) -> None:
            if self._open:
                self._open = False
                with suppress(OSError):
                    os.close(self.fd)

    def _create_new_file(root: Path, parent: ResolvedPath, name: str) -> _NewFile:
        """Create ``name`` in ``parent`` with CREATE_NEW, which never opens an existing file.

        Share mode 0 keeps other processes from opening, renaming, or deleting the
        file until it is closed. DELETE access lets ``discard`` remove it by handle.
        """
        handle = _create_file_w(
            str(parent.absolute / name),
            _GENERIC_WRITE | _DELETE | _FILE_READ_ATTRIBUTES,
            0,
            None,
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # Without os.O_TEXT the descriptor is binary: newlines are not translated.
            fd = msvcrt.open_osfhandle(handle, 0)
        except BaseException:
            _mark_for_deletion(handle)
            _close_handle(handle)
            raise
        return _NewFile(fd)

else:
    _DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    _CREATE_FLAGS = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_NOCTTY", 0)
    )

    class _NewFile:
        def __init__(self, fd: int, parent_fd: int, name: str, identity: os.stat_result) -> None:
            self.fd = fd
            self._parent_fd = parent_fd
            self._name = name
            self._identity = identity
            self._open = True

        def close_file(self) -> None:
            self._open = False
            os.close(self.fd)

        def discard(self) -> bool:
            # POSIX cannot unlink by descriptor. Unlink the name only while it still
            # refers to our file; a narrow window remains between the stat and the
            # unlink (see docs/architecture.md).
            try:
                current = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
                if not os.path.samestat(current, self._identity):
                    return False
                os.unlink(self._name, dir_fd=self._parent_fd)
            except OSError:
                return False
            return True

        def release(self) -> None:
            if self._open:
                self._open = False
                with suppress(OSError):
                    os.close(self.fd)
            with suppress(OSError):
                os.close(self._parent_fd)

    def _create_new_file(root: Path, parent: ResolvedPath, name: str) -> _NewFile:
        """Create ``name`` with O_CREAT | O_EXCL relative to a pinned parent directory.

        The parent is reached by opening each component with O_NOFOLLOW relative to
        the previous one, starting at the root, so a component swapped for a symlink
        after the ``lstat`` checks is refused instead of followed.
        """
        if os.open not in os.supports_dir_fd:
            raise WorkspaceError("creating files is not supported on this platform")
        try:
            dir_fd = os.open(root, _DIRECTORY_FLAGS)
        except OSError as exc:
            raise _os_error(exc, ".") from None
        try:
            for index, part in enumerate(parent.parts):
                prefix = "/".join(parent.parts[: index + 1])
                try:
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=dir_fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise WorkspaceError(
                            f"directory changed while the file was being created: '{prefix}'"
                        ) from None
                    raise _os_error(exc, prefix) from None
                os.close(dir_fd)
                dir_fd = next_fd
            if not os.path.samestat(os.fstat(dir_fd), parent.stat):
                raise WorkspaceError(
                    f"directory changed while the file was being created: '{parent.relative}'"
                )
            fd = os.open(name, _CREATE_FLAGS, 0o666, dir_fd=dir_fd)
        except BaseException:
            os.close(dir_fd)
            raise
        try:
            identity = os.fstat(fd)
        except OSError:
            # Without the file's identity it cannot be removed safely, so it is left.
            os.close(fd)
            os.close(dir_fd)
            relative = "/".join((*parent.parts, name))
            raise WorkspaceError(
                f"failed to write '{relative}'; the incomplete file could not be removed safely"
            ) from None
        return _NewFile(fd, dir_fd, name, identity)

"""Workspace path confinement: the security boundary for filesystem tools.

Every path a tool receives is untrusted. ``Workspace`` turns such a path into a
verified location inside the authorized workspace root, or refuses it with a
``WorkspaceError``. Error messages are safe to show to the client: they name only
the workspace-relative path and never include file contents or absolute paths.

What may be accessed is decided by ``universal_ai_tools.policy``; this module only
enforces it. Checks, in order:

1. Lexical, before touching the filesystem: the path must be relative and must not
   contain ``..`` or NUL, and the policy must allow it. On Windows, drive letters, UNC
   prefixes, alternate data streams, and reserved device names are refused.
2. Pinned traversal: from the root, each component is opened without following links
   and is verified through that open handle. A symlink, Windows junction, or other
   name-surrogate reparse point is refused. On Linux and macOS the next component is
   opened relative to the previous descriptor. On Windows every handle stays open
   without delete sharing, so no component can be renamed until the operation ends.
3. After opening a file: the descriptor must refer to a regular file, must be the file
   step 2 inspected, must have one name (a hard link can be any file on the volume),
   and its real path must lie inside the root and pass the policy again. That catches
   aliases such as Windows 8.3 short names. On Windows, Linux, and macOS the real path is
   required: if it cannot be read, the operation is refused. Other platforms cannot
   report it, and the check is skipped there.

The link count is read once, after opening. It is NOT a guarantee against a process that
creates and removes hard links while a file is being read: on Linux such a process can
make the count read 1 for a file that also has a name outside the workspace. See
docs/architecture.md.

Steps 2 and 3 run for every operation, and directories get the same real-path check,
so a listing is as confined as a read. See docs/architecture.md for the remaining
limitations.

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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from universal_ai_tools.policy import Operation, refusal

# The longest path accepted, which also bounds what a refusal can quote.
MAX_PATH_CHARS = 4096

_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_REPARSE_TAG_NAME_SURROGATE = 0x20000000


class WorkspaceError(Exception):
    """A refused or failed filesystem request. The message is safe to show to clients."""


class FileTooLargeError(WorkspaceError):
    """A file exceeds the size limit for the requested operation."""


class PathNotFoundError(WorkspaceError):
    """A path, or one of its parent directories, does not exist."""


class HardLinkError(WorkspaceError):
    """A file has several names, so the name that was checked says nothing about it."""


def _is_redirect(attributes: int, reparse_tag: int) -> bool:
    # Junctions and other name surrogates redirect to another path. Other reparse
    # points, such as OneDrive cloud files, are ordinary files and are allowed.
    return bool(
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT and reparse_tag & _REPARSE_TAG_NAME_SURROGATE
    )


def is_link(st: os.stat_result) -> bool:
    """Return whether an ``lstat`` result is a symlink, junction, or similar redirect."""
    if stat.S_ISLNK(st.st_mode):
        return True
    if sys.platform == "win32":
        return _is_redirect(st.st_file_attributes, st.st_reparse_tag)
    return False


@dataclass(frozen=True)
class ResolvedPath:
    """A location inside the workspace that passed the lexical and traversal checks."""

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


class FileReader:
    """Reads files one at a time, keeping the current parent directory pinned between them.

    A search reads many files from each directory, and walking down from the root for
    every one of them is most of its cost. Every read still verifies what it opened.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self._parts: tuple[str, ...] = ()
        self._directory: _Directory | None = None

    def read(self, target: ResolvedPath, max_bytes: int) -> bytes:
        """Read a file as ``Workspace.read_bytes`` does."""
        parts = target.parts[:-1]
        if self._directory is None or self._parts != parts:
            self.close()
            self._directory = self._workspace._pin(parts, target.relative)
            self._parts = parts
        return self._workspace._read_in(self._directory, target, max_bytes)

    def close(self) -> None:
        if self._directory is not None:
            self._directory.close()
            self._directory = None


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

    def resolve(self, raw: str, *, operation: Operation = Operation.LIST) -> ResolvedPath:
        """Validate an untrusted relative path and inspect it without following links.

        Args:
            raw: Path relative to the workspace root. ``/`` and ``\\`` are both
                separators; ``""`` and ``"."`` mean the root.
            operation: What the caller will do with the path. The policy refuses it
                before the filesystem is touched, so a refusal does not reveal whether
                the path exists.

        Raises:
            WorkspaceError: If the path is malformed, escapes the workspace, passes
                through a link, is refused by the policy, or does not exist.
        """
        parts = _split_relative(raw)
        relative = "/".join(parts) or "."
        _enforce(parts, operation, relative)
        if not parts:
            try:
                return ResolvedPath((), self._root, os.lstat(self._root))
            except OSError as exc:
                raise _os_error(exc, relative) from None
        with self._pinned(parts[:-1], relative) as parent:
            try:
                st = parent.lstat(parts[-1])
            except OSError as exc:
                raise _os_error(exc, relative) from None
        if is_link(st):
            raise WorkspaceError(f"refusing to follow symlink or junction: '{relative}'")
        return ResolvedPath(tuple(parts), self._root.joinpath(*parts), st)

    def scan_directory(self, directory: ResolvedPath) -> list[ResolvedPath]:
        """Return the entries of ``directory``, sorted by name, without following links.

        Entries that vanish while being listed are omitted.
        """
        if not directory.is_dir:
            raise WorkspaceError(f"not a directory: '{directory.relative}'")
        children: list[ResolvedPath] = []
        with self._pinned(directory.parts, directory.relative) as pinned:
            try:
                found = sorted(_scan_entries(pinned), key=lambda entry: entry.name)
            except OSError as exc:
                raise _os_error(exc, directory.relative) from None
            for entry in found:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                children.append(
                    ResolvedPath(
                        (*directory.parts, entry.name), directory.absolute / entry.name, st
                    )
                )
        return children

    def read_bytes(self, target: ResolvedPath, max_bytes: int) -> bytes:
        """Read a regular file of at most ``max_bytes`` bytes after verifying what was opened.

        Raises:
            FileTooLargeError: If the file is larger than ``max_bytes``.
            HardLinkError: If the file has more than one name.
            WorkspaceError: If the target is not a regular file inside the workspace,
                changed while being opened, is refused by the policy, or cannot be read.
        """
        with self._pinned(target.parts[:-1], target.relative) as parent:
            return self._read_in(parent, target, max_bytes)

    @contextmanager
    def file_reader(self) -> Iterator[FileReader]:
        """Return a reader for many files, for operations that walk a directory tree."""
        reader = FileReader(self)
        try:
            yield reader
        finally:
            reader.close()

    def _read_in(self, parent: _Directory, target: ResolvedPath, max_bytes: int) -> bytes:
        relative = target.relative
        if target.is_link:
            raise WorkspaceError(f"refusing to follow symlink or junction: '{relative}'")
        if target.is_dir:
            raise WorkspaceError(f"is a directory, not a file: '{relative}'")
        if not target.is_file:
            raise WorkspaceError(f"not a regular file: '{relative}'")

        try:
            fd = _open_file(parent, target.name)
        except OSError as exc:
            raise _os_error(exc, relative) from None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or not _same_file(target.stat, opened):
                raise WorkspaceError(f"file changed while being opened: '{relative}'")
            real = _real_path_of_fd(fd)
            if real is not None:
                self._require_allowed(real, relative, Operation.READ)
            if opened.st_nlink > 1:
                raise HardLinkError(f"refusing to read hard-linked file: '{relative}'")
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

    def create_file(self, raw: str, data: bytes) -> str:
        """Create a new file containing ``data``. Never replaces anything that exists.

        Args:
            raw: Untrusted path of the new file, relative to the workspace root. Its
                parent directory must already exist.
            data: The complete file contents.

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
        _enforce(parts, Operation.CREATE, relative)

        name = parts[-1]
        parent_relative = "/".join(parts[:-1]) or "."
        try:
            with self._pinned(parts[:-1], relative) as parent:
                self._create_in(parent, name, relative, data)
        except PathNotFoundError:
            raise WorkspaceError(f"parent directory does not exist: '{parent_relative}'") from None
        return relative

    def _create_in(self, parent: _Directory, name: str, relative: str, data: bytes) -> None:
        try:
            real_parent = parent.real_path()
        except OSError as exc:
            raise _os_error(exc, relative) from None
        if real_parent is not None:
            # The real path also catches aliases such as the 8.3 short name GIT~1 for .git.
            self._require_allowed(os.path.join(real_parent, name), relative, Operation.CREATE)
        if _exists(parent, name, relative):
            raise WorkspaceError(f"already exists: '{relative}'")
        try:
            new = _create_new_file(parent, name)
        except OSError as exc:
            # Something appeared since the check. Windows reports an existing directory
            # or junction as access denied rather than as already existing.
            if isinstance(exc, FileExistsError) or _exists(parent, name, relative):
                raise WorkspaceError(f"already exists: '{relative}'") from None
            raise _os_error(exc, relative) from None

        try:
            self._verify_created(new.fd, real_parent, name, relative)
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

    def _verify_created(self, fd: int, real_parent: str | None, name: str, relative: str) -> None:
        """Check, before writing, that the new file landed where it was meant to."""
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkspaceError(f"file changed while being created: '{relative}'")
        real = _real_path_of_fd(fd)
        if real is None:
            # Only possible where the pinned descriptor is the only evidence of location.
            return
        self._require_allowed(real, relative, Operation.CREATE)
        # A pinned parent can still be renamed on POSIX. The file would then be somewhere
        # other than the path that was checked, so it must be exactly where it was meant to be.
        if real_parent is not None and Path(real) != Path(real_parent) / name:
            raise WorkspaceError(
                f"directory changed while the file was being created: '{relative}'"
            )

    @contextmanager
    def _pinned(self, parts: Sequence[str], relative: str) -> Iterator[_Directory]:
        """Hold the directory ``parts`` open for the duration of the ``with`` block."""
        directory = self._pin(parts, relative)
        try:
            yield directory
        finally:
            directory.close()

    def _pin(self, parts: Sequence[str], relative: str) -> _Directory:
        """Open the directory ``parts`` from the root, verifying every step on the way.

        Everything that touches an entry of it goes through this handle or, on Windows,
        through a path whose every component is held open.
        """
        try:
            directory = _open_root(self._root)
        except OSError as exc:
            raise _os_error(exc, relative) from None
        try:
            for index, part in enumerate(parts):
                directory.descend(part, "/".join(parts[: index + 1]))
            real = directory.real_path()
            if real is not None:
                self._require_allowed(real, relative, Operation.LIST)
        except OSError as exc:
            directory.close()
            raise _os_error(exc, relative) from None
        except BaseException:
            directory.close()
            raise
        return directory

    def _require_allowed(self, real: str, relative: str, operation: Operation) -> None:
        """Refuse a real (fully resolved) path outside the root or refused by the policy.

        Checking the real path also catches aliases that lexical checks cannot,
        such as Windows 8.3 short names.
        """
        try:
            inside = Path(real).relative_to(self._root).parts
        except ValueError:
            raise WorkspaceError(f"path resolves outside the workspace: '{relative}'") from None
        _enforce(inside, operation, relative)


def _enforce(parts: Sequence[str], operation: Operation, relative: str) -> None:
    reason = refusal(parts, operation)
    if reason is not None:
        raise WorkspaceError(f"{reason}: '{relative}'")


def _split_relative(raw: str) -> list[str]:
    if "\x00" in raw:
        raise WorkspaceError("path must not contain NUL characters")
    if len(raw) > MAX_PATH_CHARS:
        raise WorkspaceError(f"path is too long: the limit is {MAX_PATH_CHARS} characters")
    if (
        raw.startswith(("/", "\\"))
        or PurePosixPath(raw).is_absolute()
        or PureWindowsPath(raw).drive
    ):
        raise WorkspaceError(
            "path must be relative to the workspace root; absolute paths, drive letters, "
            "and UNC paths are not allowed"
        )
    try:
        os.fsencode(raw)
    except UnicodeEncodeError:
        raise WorkspaceError("path is not valid text for this filesystem") from None
    parts = [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]
    if ".." in parts:
        raise WorkspaceError("path must not contain '..' components")
    if sys.platform == "win32":
        for part in parts:
            if ":" in part or os.path.isreserved(part):
                raise WorkspaceError(f"path component is not allowed on Windows: '{part}'")
    return parts


def _os_error(exc: OSError, relative: str) -> WorkspaceError:
    # Never include exc.filename or str(exc): both can contain absolute paths.
    if isinstance(exc, FileNotFoundError):
        return PathNotFoundError(f"not found: '{relative}'")
    if exc.errno == errno.ELOOP:
        return WorkspaceError(f"refusing to follow symlink or junction: '{relative}'")
    if isinstance(exc, NotADirectoryError):
        return WorkspaceError(f"not a directory: '{relative}'")
    if isinstance(exc, PermissionError):
        return WorkspaceError(f"permission denied: '{relative}'")
    return WorkspaceError(f"cannot access '{relative}'")


def _same_file(expected: os.stat_result, opened: os.stat_result) -> bool:
    # Windows directory scans report no file ID, so a scanned entry has no identity to
    # compare. Everything else read_bytes checks is checked on the open file itself.
    return expected.st_ino == 0 or os.path.samestat(expected, opened)


def _exists(directory: _Directory, name: str, relative: str) -> bool:
    """Return whether anything, including a link, exists at ``name``."""
    try:
        directory.lstat(name)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _os_error(exc, relative) from None
    return True


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

    def _final_path(handle: int) -> str:
        """Return the real path of an open handle, as the OS reports it."""
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

    def _real_path_of_fd(fd: int) -> str | None:
        """Return the real path of an open file, as the OS reports it."""
        return _final_path(msvcrt.get_osfhandle(fd))

elif sys.platform == "linux":

    def _real_path_of_fd(fd: int) -> str:
        """Return the real path of an open file or directory, as /proc reports it.

        This fails closed. Without the real path the checks that depend on it cannot run,
        so any failure refuses the operation: /proc not mounted, or a path longer than the
        kernel will report, which a process that can rename files can bring about.
        """
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            raise WorkspaceError(
                "cannot verify the location of an open file or directory, because its "
                "path cannot be read from /proc/self/fd; refusing access"
            ) from None

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


# Pinned traversal. _open_root returns a _Directory for the root, and ``descend`` moves
# it one verified component down. _open_file and _create_new_file work on its entries.
#
# _create_new_file returns a _NewFile whose ``fd`` is open for writing. ``discard`` removes
# the file only if it is still the one this process created; ``release`` closes the file.

if sys.platform == "win32":
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_ATTRIBUTES = 0x0080
    _SYNCHRONIZE = 0x00100000
    _FILE_SHARE_READ_WRITE = 0x0003
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _FILE_DISPOSITION_INFO_CLASS = 4
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = (("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD))

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
    _get_file_information = _kernel32.GetFileInformationByHandleEx
    _get_file_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _get_file_information.restype = wintypes.BOOL
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

    def _open_handle(path: str, access: int, share: int, disposition: int, flags: int) -> int:
        handle: int = _create_file_w(path, access, share, None, disposition, flags, None)
        if handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _attributes_of(handle: int) -> _AttributeTagInfo:
        info = _AttributeTagInfo()
        if not _get_file_information(
            handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info

    def _open_directory_handle(path: str) -> int:
        """Open a directory without following a link, and without allowing deletion.

        The sharing rule only binds handles with data access, which is why this asks for
        FILE_LIST_DIRECTORY. While it is open, without FILE_SHARE_DELETE, nothing can
        rename or delete this directory or any directory above it.
        """
        return _open_handle(
            path,
            _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            _FILE_SHARE_READ_WRITE,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        )

    class _Directory:
        """Open handles for each directory from the root down to this one.

        Windows has no ``openat`` in its public API. Holding every component open has
        the same effect: ``path`` cannot come to mean anything else while they are held.
        """

        def __init__(self, handle: int, path: str) -> None:
            self.path = path
            self._handles = [handle]

        def child(self, name: str) -> str:
            return f"{self.path}\\{name}"

        def lstat(self, name: str) -> os.stat_result:
            return os.lstat(self.child(name))

        def real_path(self) -> str | None:
            return _final_path(self._handles[-1])

        def descend(self, name: str, prefix: str) -> None:
            handle = _open_child(self, name)
            try:
                info = _attributes_of(handle)
                if _is_redirect(info.attributes, info.reparse_tag):
                    raise WorkspaceError(f"refusing to follow symlink or junction: '{prefix}'")
                if not info.attributes & _FILE_ATTRIBUTE_DIRECTORY:
                    raise WorkspaceError(f"not a directory: '{prefix}'")
            except BaseException:
                _close_handle(handle)
                raise
            self._handles.append(handle)
            self.path = self.child(name)

        def close(self) -> None:
            while self._handles:
                _close_handle(self._handles.pop())

    def _open_root(root: Path) -> _Directory:
        path = str(root)
        return _Directory(_open_directory_handle(path), path)

    def _open_child(directory: _Directory, name: str) -> int:
        return _open_directory_handle(directory.child(name))

    def _scan_entries(directory: _Directory) -> list[os.DirEntry[str]]:
        with os.scandir(directory.path) as iterator:
            return list(iterator)

    def _open_file(directory: _Directory, name: str) -> int:
        """Open a file for reading without following a link, and return a descriptor."""
        handle = _open_handle(
            directory.child(name),
            _GENERIC_READ,
            _FILE_SHARE_READ_WRITE,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            info = _attributes_of(handle)
            if _is_redirect(info.attributes, info.reparse_tag):
                raise OSError(errno.ELOOP, "refusing to follow a link")
            # Without os.O_TEXT the descriptor is binary: newlines are not translated.
            return msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            _close_handle(handle)
            raise

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

    def _create_new_file(directory: _Directory, name: str) -> _NewFile:
        """Create ``name`` with CREATE_NEW, which never opens an existing file.

        Share mode 0 keeps other processes from opening, renaming, or deleting the
        file until it is closed. DELETE access lets ``discard`` remove it by handle.
        """
        handle = _open_handle(
            directory.child(name),
            _GENERIC_WRITE | _DELETE | _FILE_READ_ATTRIBUTES,
            0,
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
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
    _OPEN_FLAGS = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        # Never block on a FIFO or claim a terminal, should one slip past the type checks.
        | os.O_NONBLOCK
        | getattr(os, "O_NOCTTY", 0)
    )
    _CREATE_FLAGS = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_NOCTTY", 0)
    )

    class _Directory:
        """A descriptor reached from the root by ``openat`` with ``O_NOFOLLOW`` at each step.

        Each component is a single name with no ``/`` or ``..``, so this walk gives what
        ``openat2(RESOLVE_NO_SYMLINKS | RESOLVE_BENEATH)`` would, on every kernel.
        """

        def __init__(self, fd: int) -> None:
            self.fd = fd

        def lstat(self, name: str) -> os.stat_result:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

        def real_path(self) -> str | None:
            return _real_path_of_fd(self.fd)

        def descend(self, name: str, prefix: str) -> None:
            st = self.lstat(name)
            if is_link(st):
                raise WorkspaceError(f"refusing to follow symlink or junction: '{prefix}'")
            if not stat.S_ISDIR(st.st_mode):
                raise WorkspaceError(f"not a directory: '{prefix}'")
            try:
                fd = _open_child(self, name)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise WorkspaceError(f"directory changed while in use: '{prefix}'") from None
                raise
            try:
                if not os.path.samestat(os.fstat(fd), st):
                    raise WorkspaceError(f"directory changed while in use: '{prefix}'")
            except BaseException:
                os.close(fd)
                raise
            os.close(self.fd)
            self.fd = fd

        def close(self) -> None:
            os.close(self.fd)

    def _open_root(root: Path) -> _Directory:
        if os.open not in os.supports_dir_fd:
            raise WorkspaceError("this platform cannot confine filesystem access")
        return _Directory(os.open(root, _DIRECTORY_FLAGS))

    def _open_child(directory: _Directory, name: str) -> int:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=directory.fd)

    def _scan_entries(directory: _Directory) -> list[os.DirEntry[str]]:
        with os.scandir(directory.fd) as iterator:
            return list(iterator)

    def _open_file(directory: _Directory, name: str) -> int:
        """Open a file for reading without following a link, and return a descriptor."""
        return os.open(name, _OPEN_FLAGS, dir_fd=directory.fd)

    class _NewFile:
        def __init__(
            self, fd: int, directory: _Directory, name: str, identity: os.stat_result
        ) -> None:
            self.fd = fd
            self._directory = directory
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
                if not os.path.samestat(self._directory.lstat(self._name), self._identity):
                    return False
                os.unlink(self._name, dir_fd=self._directory.fd)
            except OSError:
                return False
            return True

        def release(self) -> None:
            if self._open:
                self._open = False
                with suppress(OSError):
                    os.close(self.fd)

    def _create_new_file(directory: _Directory, name: str) -> _NewFile:
        """Create ``name`` with O_CREAT | O_EXCL relative to the pinned directory."""
        fd = os.open(name, _CREATE_FLAGS, 0o666, dir_fd=directory.fd)
        try:
            identity = os.fstat(fd)
        except OSError:
            # Without the file's identity it cannot be removed safely, so it is left.
            os.close(fd)
            raise WorkspaceError(
                "failed to write the new file; the incomplete file could not be removed safely"
            ) from None
        return _NewFile(fd, directory, name, identity)

"""Tests for the filesystem tools and the workspace confinement they rely on.

Every test builds a synthetic workspace under pytest's tmp_path. Nothing here
reads real user files.
"""

from __future__ import annotations

import errno
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from support import link_directory, link_file, short_name, write
from universal_ai_tools import workspace as workspace_module
from universal_ai_tools.config import ENV_WORKSPACE, load_config
from universal_ai_tools.filesystem import (
    FilesystemLimits,
    list_files,
    read_file,
    search_files,
    write_file,
)
from universal_ai_tools.workspace import Workspace, WorkspaceError

OUTSIDE_MARKER = "TOP-OUTSIDE_MARKER-OUTSIDE-CONTENT"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def workspace(root: Path) -> Workspace:
    return Workspace(load_config({ENV_WORKSPACE: str(root)}).workspace_root)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    path = tmp_path / "outside"
    path.mkdir()
    (path / "secret.txt").write_text(f"{OUTSIDE_MARKER}\n", encoding="utf-8")
    return path


def refusal(excinfo: pytest.ExceptionInfo[WorkspaceError], *, tmp_path: Path) -> str:
    """Return the error message after checking it leaks no absolute paths or secrets."""
    message = str(excinfo.value)
    assert str(tmp_path) not in message
    assert str(tmp_path.resolve()) not in message
    assert OUTSIDE_MARKER not in message
    return message


# list_files


def test_list_root(workspace: Workspace, root: Path) -> None:
    write(root / "b.txt", "abc")
    write(root / "sub" / "c.txt", "c")
    (root / "empty").mkdir()

    result = list_files(workspace)

    assert result.path == "."
    assert [(e.path, e.type, e.size_bytes) for e in result.entries] == [
        ("b.txt", "file", 3),
        ("empty", "directory", None),
        ("sub", "directory", None),
    ]
    assert result.truncated is False


@pytest.mark.parametrize("path", ["sub", "sub/", "./sub", "sub\\", "sub//"])
def test_list_subdirectory(workspace: Workspace, root: Path, path: str) -> None:
    write(root / "sub" / "deeper" / "x.py", "x")

    result = list_files(workspace, path)

    assert result.path == "sub"
    assert [e.path for e in result.entries] == ["sub/deeper"]


def test_list_recursive_is_depth_first_and_skips_excluded(workspace: Workspace, root: Path) -> None:
    write(root / "a" / "one.txt", "1")
    write(root / "a" / "b" / "two.txt", "2")
    write(root / "z.txt", "z")
    write(root / ".git" / "config", "[core]")
    write(root / ".venv" / "lib" / "x.py", "x")
    write(root / "node_modules" / "pkg" / "index.js", "x")

    result = list_files(workspace, recursive=True)

    assert [e.path for e in result.entries] == [
        ".git",
        ".venv",
        "a",
        "a/b",
        "a/b/two.txt",
        "a/one.txt",
        "node_modules",
        "z.txt",
    ]
    assert result.skipped_directories == 3


def test_list_excluded_directory_explicitly(workspace: Workspace, root: Path) -> None:
    write(root / ".venv" / "pyvenv.cfg", "home = /usr")

    result = list_files(workspace, ".venv")

    assert [e.path for e in result.entries] == [".venv/pyvenv.cfg"]


def test_list_empty_directory(workspace: Workspace, root: Path) -> None:
    (root / "empty").mkdir()

    assert list_files(workspace, "empty").entries == []
    assert list_files(workspace).entries[0].path == "empty"


def test_list_empty_workspace(workspace: Workspace) -> None:
    result = list_files(workspace, recursive=True)

    assert result.entries == []
    assert result.truncated is False


def test_list_truncates_at_max_entries(workspace: Workspace, root: Path) -> None:
    for index in range(5):
        write(root / f"f{index}.txt", "")

    result = list_files(workspace, max_entries=3)

    assert [e.path for e in result.entries] == ["f0.txt", "f1.txt", "f2.txt"]
    assert result.truncated is True


def test_list_exact_fit_is_not_truncated(workspace: Workspace, root: Path) -> None:
    for index in range(3):
        write(root / f"f{index}.txt", "")

    result = list_files(workspace, max_entries=3)

    assert len(result.entries) == 3
    assert result.truncated is False


def test_list_max_entries_is_capped_by_limits(workspace: Workspace, root: Path) -> None:
    for index in range(5):
        write(root / f"f{index}.txt", "")

    result = list_files(workspace, max_entries=1000, limits=FilesystemLimits(max_list_entries=2))

    assert len(result.entries) == 2
    assert result.truncated is True


def test_list_rejects_non_positive_max_entries(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="max_entries must be at least 1"):
        list_files(workspace, max_entries=0)


def test_list_missing_directory(workspace: Workspace, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        list_files(workspace, "nope")

    assert refusal(excinfo, tmp_path=tmp_path) == "not found: 'nope'"


def test_list_file_is_not_a_directory(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "a")

    with pytest.raises(WorkspaceError, match=re.escape("not a directory: 'a.txt'")):
        list_files(workspace, "a.txt")


def test_list_reports_directory_link_without_following(
    workspace: Workspace, root: Path, outside: Path, tmp_path: Path
) -> None:
    link_directory(root / "escape", outside)

    result = list_files(workspace, recursive=True)

    assert [(e.path, e.type) for e in result.entries] == [("escape", "symlink")]
    with pytest.raises(WorkspaceError) as excinfo:
        list_files(workspace, "escape")
    assert "refusing to follow symlink or junction: 'escape'" in refusal(excinfo, tmp_path=tmp_path)


def test_list_reports_file_symlink(workspace: Workspace, root: Path, outside: Path) -> None:
    link_file(root / "secret-link.txt", outside / "secret.txt")

    result = list_files(workspace)

    assert [(e.path, e.type, e.size_bytes) for e in result.entries] == [
        ("secret-link.txt", "symlink", None)
    ]


def test_list_skips_unreadable_subdirectory(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "locked" / "hidden.txt", "x")
    write(root / "open" / "visible.txt", "x")
    real_scan = workspace_module._scan_entries

    def scan(directory: workspace_module._Directory) -> list[os.DirEntry[str]]:
        real = directory.real_path()
        assert real is not None
        if Path(real).name == "locked":
            raise PermissionError(13, "Permission denied", real)
        return real_scan(directory)

    monkeypatch.setattr(workspace_module, "_scan_entries", scan)

    result = list_files(workspace, recursive=True)

    assert [e.path for e in result.entries] == ["locked", "open", "open/visible.txt"]
    assert result.skipped_directories == 1


# read_file


def test_read_file(workspace: Workspace, root: Path) -> None:
    write(root / "notes" / "hello.md", "héllo\r\nworld\n")

    result = read_file(workspace, "notes/hello.md")

    assert result.path == "notes/hello.md"
    assert result.content == "héllo\r\nworld\n"
    assert result.size_bytes == len("héllo\r\nworld\n".encode())


@pytest.mark.parametrize(
    ("raw", "content"),
    [
        (b"hello sandbox\r\n", "hello sandbox\r\n"),
        (b"\xef\xbb\xbfhello sandbox\r\n", "hello sandbox\r\n"),
        (b"\xef\xbb\xbf\xef\xbb\xbfdouble", "﻿double"),
        (b"inner \xef\xbb\xbf kept", "inner ﻿ kept"),
    ],
)
def test_read_file_strips_only_a_leading_bom(
    workspace: Workspace, root: Path, raw: bytes, content: str
) -> None:
    write(root / "notes" / "hello.txt", raw)

    result = read_file(workspace, "notes/hello.txt")

    assert result.content == content
    assert result.size_bytes == len(raw)


def test_read_file_with_backslashes(workspace: Workspace, root: Path) -> None:
    write(root / "a" / "b.txt", "ok")

    assert read_file(workspace, "a\\b.txt").content == "ok"


def test_read_empty_file(workspace: Workspace, root: Path) -> None:
    write(root / "empty.txt", "")

    result = read_file(workspace, "empty.txt")

    assert result.content == ""
    assert result.size_bytes == 0


def test_read_env_template_is_allowed(workspace: Workspace, root: Path) -> None:
    write(root / ".env.example", "KEY=placeholder")

    assert read_file(workspace, ".env.example").content == "KEY=placeholder"


@pytest.mark.parametrize("path", ["sub", "sub/", ".", ""])
def test_read_directory_is_refused(workspace: Workspace, root: Path, path: str) -> None:
    (root / "sub").mkdir()

    with pytest.raises(WorkspaceError, match="is a directory, not a file"):
        read_file(workspace, path)


def test_read_missing_file(workspace: Workspace, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "missing.txt")

    assert refusal(excinfo, tmp_path=tmp_path) == "not found: 'missing.txt'"


def test_read_through_file_component(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "a")

    with pytest.raises(WorkspaceError, match=re.escape("not a directory: 'a.txt'")):
        read_file(workspace, "a.txt/b.txt")


def test_read_binary_file_is_refused(workspace: Workspace, root: Path, tmp_path: Path) -> None:
    write(root / "image.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "image.png")

    assert "appears to be binary" in refusal(excinfo, tmp_path=tmp_path)


def test_read_invalid_utf8_is_refused(workspace: Workspace, root: Path, tmp_path: Path) -> None:
    write(root / "latin1.txt", b"caf\xe9 " + OUTSIDE_MARKER.encode())

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "latin1.txt")

    message = refusal(excinfo, tmp_path=tmp_path)
    assert message == "file is not valid UTF-8 text: 'latin1.txt' (invalid byte at offset 3)"


def test_read_invalid_utf8_after_bom_is_refused(
    workspace: Workspace, root: Path, tmp_path: Path
) -> None:
    write(root / "latin1.txt", b"\xef\xbb\xbfcaf\xe9 " + OUTSIDE_MARKER.encode())

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "latin1.txt")

    # The offset counts raw bytes, including the 3-byte BOM.
    message = refusal(excinfo, tmp_path=tmp_path)
    assert message == "file is not valid UTF-8 text: 'latin1.txt' (invalid byte at offset 6)"


def test_read_oversized_file_is_refused(workspace: Workspace, root: Path) -> None:
    limits = FilesystemLimits(max_read_bytes=10)
    write(root / "fits.txt", "x" * 10)
    write(root / "big.txt", "x" * 11)

    assert read_file(workspace, "fits.txt", limits=limits).size_bytes == 10
    with pytest.raises(WorkspaceError, match=re.escape("file is too large: 'big.txt' is 11 bytes")):
        read_file(workspace, "big.txt", limits=limits)


def test_read_default_size_limit(workspace: Workspace, root: Path) -> None:
    write(root / "big.txt", b"x" * (1024 * 1024 + 1))

    with pytest.raises(WorkspaceError, match="the limit is 1048576 bytes"):
        read_file(workspace, "big.txt")


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "\\Windows\\win.ini",
        "C:\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "C:relative.txt",
        "\\\\server\\share\\file.txt",
        "//server/share/file.txt",
        "\\\\?\\C:\\Windows\\win.ini",
    ],
)
def test_read_absolute_paths_are_refused(workspace: Workspace, path: str, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, path)

    assert "path must be relative" in refusal(excinfo, tmp_path=tmp_path)


def test_read_absolute_path_inside_workspace_is_refused(workspace: Workspace, root: Path) -> None:
    target = write(root / "a.txt", "a")

    with pytest.raises(WorkspaceError, match="path must be relative"):
        read_file(workspace, str(target))


def test_read_absolute_path_outside_workspace_is_refused(
    workspace: Workspace, outside: Path, tmp_path: Path
) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, str(outside / "secret.txt"))

    assert "path must be relative" in refusal(excinfo, tmp_path=tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../outside/secret.txt",
        "..\\outside\\secret.txt",
        "sub/../../outside/secret.txt",
        "sub/..",
        "./../outside/secret.txt",
    ],
)
def test_read_traversal_is_refused(
    workspace: Workspace, root: Path, outside: Path, path: str, tmp_path: Path
) -> None:
    (root / "sub").mkdir()

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, path)

    assert refusal(excinfo, tmp_path=tmp_path) == "path must not contain '..' components"


def test_read_nul_in_path_is_refused(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="NUL"):
        read_file(workspace, "a.txt\x00.png")


def test_read_file_symlink_inside_workspace_is_refused(workspace: Workspace, root: Path) -> None:
    write(root / "real.txt", "inside")
    link_file(root / "alias.txt", root / "real.txt")

    with pytest.raises(
        WorkspaceError, match=re.escape("refusing to follow symlink or junction: 'alias.txt'")
    ):
        read_file(workspace, "alias.txt")


def test_read_file_symlink_outside_workspace_is_refused(
    workspace: Workspace, root: Path, outside: Path, tmp_path: Path
) -> None:
    link_file(root / "secret.txt", outside / "secret.txt")

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "secret.txt")

    assert "refusing to follow symlink" in refusal(excinfo, tmp_path=tmp_path)


def test_read_through_directory_link_outside_is_refused(
    workspace: Workspace, root: Path, outside: Path, tmp_path: Path
) -> None:
    link_directory(root / "escape", outside)

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "escape/secret.txt")

    assert refusal(excinfo, tmp_path=tmp_path) == (
        "refusing to follow symlink or junction: 'escape'"
    )


def test_read_through_directory_link_inside_is_refused(workspace: Workspace, root: Path) -> None:
    write(root / "real" / "a.txt", "a")
    link_directory(root / "alias", root / "real")

    with pytest.raises(WorkspaceError, match="refusing to follow symlink or junction: 'alias'"):
        read_file(workspace, "alias/a.txt")


@pytest.mark.parametrize(
    "path",
    [".env", ".ENV", ".env.local", "config/.env.production", "id_rsa", "server.pem", "tls.KEY"],
)
def test_read_sensitive_files_are_refused(workspace: Workspace, root: Path, path: str) -> None:
    write(root / path, "API_KEY=synthetic-not-a-secret")

    with pytest.raises(WorkspaceError, match="reading sensitive files is not allowed"):
        read_file(workspace, path)


def test_sensitive_refusal_does_not_reveal_existence(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="reading sensitive files is not allowed"):
        read_file(workspace, "missing/.env")


@pytest.mark.parametrize("path", [".ssh/config", "nested/.SSH/known_hosts", ".aws/credentials"])
def test_sensitive_directories_are_refused(workspace: Workspace, root: Path, path: str) -> None:
    write(root / path, "synthetic")

    with pytest.raises(WorkspaceError, match="access to sensitive directories is not allowed"):
        read_file(workspace, path)
    with pytest.raises(WorkspaceError, match="access to sensitive directories is not allowed"):
        list_files(workspace, str(Path(path).parent))


def test_read_permission_error_is_reported_safely(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write(root / "locked.txt", "contents")
    real_open = workspace_module._open_file

    def deny(directory: workspace_module._Directory, name: str) -> int:
        if name == "locked.txt":
            raise PermissionError(13, "Permission denied", str(root / name))
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", deny)

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "locked.txt")

    assert refusal(excinfo, tmp_path=tmp_path) == "permission denied: 'locked.txt'"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_read_unreadable_file_posix(workspace: Workspace, root: Path, tmp_path: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses permission bits")
    target = write(root / "locked.txt", "contents")
    target.chmod(0)
    try:
        with pytest.raises(WorkspaceError) as excinfo:
            read_file(workspace, "locked.txt")
    finally:
        target.chmod(0o600)

    assert refusal(excinfo, tmp_path=tmp_path) == "permission denied: 'locked.txt'"


def test_file_swapped_after_check_is_refused(
    workspace: Workspace,
    root: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A real race: the checked file is replaced on disk just before it is opened."""
    write(root / "a.txt", "inside")
    replacement = write(root / "replacement.txt", f"{OUTSIDE_MARKER}\n")
    real_open = workspace_module._open_file

    def swap(directory: workspace_module._Directory, name: str) -> int:
        os.replace(replacement, root / "a.txt")
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", swap)

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "a.txt")

    assert refusal(excinfo, tmp_path=tmp_path) == "file changed while being opened: 'a.txt'"


@pytest.mark.skipif(
    sys.platform not in ("win32", "linux", "darwin"), reason="no real-path lookup for open files"
)
def test_opened_file_outside_workspace_is_refused(
    workspace: Workspace,
    root: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Even if the identity check were fooled, the opened file's real path is verified."""
    write(root / "a.txt", "inside")

    def escape(directory: workspace_module._Directory, name: str) -> int:
        return os.open(outside / "secret.txt", os.O_RDONLY | getattr(os, "O_BINARY", 0))

    monkeypatch.setattr(workspace_module, "_open_file", escape)
    monkeypatch.setattr(workspace_module, "_same_file", lambda _a, _b: True)

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "a.txt")

    assert refusal(excinfo, tmp_path=tmp_path) == "path resolves outside the workspace: 'a.txt'"


UNVERIFIABLE_LOCATION = (
    "cannot verify the location of an open file or directory, because its path cannot be "
    "read from /proc/self/fd; refusing access"
)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux reads real paths from /proc")
def test_unreadable_proc_refuses_instead_of_skipping_checks(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without /proc the real-path checks cannot run, so nothing is served."""
    write(root / "sub" / "a.txt", "inside")
    real_readlink = os.readlink

    def no_proc(path: str, *, dir_fd: int | None = None) -> str:
        if path.startswith("/proc/"):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        return real_readlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "readlink", no_proc)

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "sub/a.txt")
    assert refusal(excinfo, tmp_path=tmp_path) == UNVERIFIABLE_LOCATION
    with pytest.raises(WorkspaceError) as excinfo:
        list_files(workspace, "sub")
    assert refusal(excinfo, tmp_path=tmp_path) == UNVERIFIABLE_LOCATION


@pytest.mark.skipif(sys.platform != "linux", reason="Linux reads real paths from /proc")
def test_real_path_too_long_to_report_is_refused(
    workspace: Workspace, root: Path, tmp_path: Path
) -> None:
    """A path the kernel will not report fails closed rather than skipping the checks.

    A process that can rename directories can push any file past this limit, so the limit
    must never switch the real-path checks off.
    """
    file_name = "f" * 200 + ".txt"
    folder_length = workspace_module.MAX_PATH_CHARS - 1 - len(file_name)
    parts = ["d" * 200] * ((folder_length + 1) // 201)
    parts.append("e" * (folder_length - len("/".join(parts)) - 1))
    folder = "/".join(parts)
    (root / folder).mkdir(parents=True)
    directory = os.open(root / folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        # The file's absolute path is too long to pass to open(), so go through its directory.
        fd = os.open(file_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        os.write(fd, b"inside")
        os.close(fd)
    finally:
        os.close(directory)
    relative = f"{folder}/{file_name}"
    assert len(relative) <= workspace_module.MAX_PATH_CHARS
    assert len(os.fsencode(root / relative)) >= 4096, "longer than Linux reports from /proc"

    # The directory's own path can still be reported, so only the file is refused.
    assert [entry.path for entry in list_files(workspace, folder).entries] == [relative]
    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, relative)
    assert refusal(excinfo, tmp_path=tmp_path) == UNVERIFIABLE_LOCATION


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path rules")
@pytest.mark.parametrize("path", ["NUL", "con.txt", "sub/COM1", "a.txt:stream", "a.txt.", "a.txt "])
def test_windows_reserved_names_are_refused(workspace: Workspace, path: str) -> None:
    with pytest.raises(WorkspaceError, match="path component is not allowed on Windows"):
        read_file(workspace, path)


# search_files


def test_search_finds_literal_matches(workspace: Workspace, root: Path) -> None:
    write(root / "src" / "app.py", "import os\n\ndef main():\n    return TODO_marker\n")
    write(root / "README.md", "TODO_marker: write docs\r\nnothing here\r\n")
    write(root / "notes.txt", "no match")

    result = search_files(workspace, "TODO_marker")

    assert [(m.path, m.line, m.column, m.snippet) for m in result.matches] == [
        ("README.md", 1, 1, "TODO_marker: write docs"),
        ("src/app.py", 4, 12, "return TODO_marker"),
    ]
    assert result.files_scanned == 3
    assert result.truncated is False
    assert result.truncation_reason is None


def test_search_columns_ignore_a_leading_bom(workspace: Workspace, root: Path) -> None:
    write(root / "bom.txt", b"\xef\xbb\xbfhello sandbox\r\nsay hello sandbox\r\n")
    write(root / "plain.txt", b"hello sandbox\nsay hello sandbox\n")

    result = search_files(workspace, "hello sandbox")

    assert [(m.path, m.line, m.column, m.snippet) for m in result.matches] == [
        ("bom.txt", 1, 1, "hello sandbox"),
        ("bom.txt", 2, 5, "say hello sandbox"),
        ("plain.txt", 1, 1, "hello sandbox"),
        ("plain.txt", 2, 5, "say hello sandbox"),
    ]


def test_search_skips_invalid_utf8_after_bom(workspace: Workspace, root: Path) -> None:
    write(root / "latin1.txt", b"\xef\xbb\xbfneedle caf\xe9")

    result = search_files(workspace, "needle")

    assert result.matches == []
    assert result.files_scanned == 1
    assert result.skipped.not_utf8 == 1


def test_search_is_literal_not_regex(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "abc\na.c\n(unclosed[\n")

    assert [m.line for m in search_files(workspace, "a.c").matches] == [2]
    assert [m.line for m in search_files(workspace, "(unclosed[").matches] == [3]


def test_search_case_sensitivity(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "Hello\nhello\nHELLO\n")

    assert len(search_files(workspace, "hello").matches) == 1
    assert len(search_files(workspace, "hello", case_sensitive=False).matches) == 3


def test_search_subdirectory(workspace: Workspace, root: Path) -> None:
    write(root / "a" / "x.txt", "needle")
    write(root / "b" / "y.txt", "needle")

    result = search_files(workspace, "needle", "b")

    assert result.path == "b"
    assert [m.path for m in result.matches] == ["b/y.txt"]


def test_search_empty_directory_and_empty_file(workspace: Workspace, root: Path) -> None:
    (root / "empty").mkdir()
    write(root / "blank.txt", "")

    empty_dir = search_files(workspace, "x", "empty")
    with_file = search_files(workspace, "x")

    assert empty_dir.matches == []
    assert empty_dir.files_scanned == 0
    assert with_file.matches == []
    assert with_file.files_scanned == 1


def test_search_skips_excluded_directories(workspace: Workspace, root: Path) -> None:
    for directory in [
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        "pkg.egg-info",
        ".pytest_cache",
    ]:
        write(root / directory / "file.txt", "needle")
    write(root / "src" / "file.txt", "needle")

    result = search_files(workspace, "needle")

    assert [m.path for m in result.matches] == ["src/file.txt"]
    assert result.skipped.excluded_directories == 6


def test_search_skips_sensitive_files(workspace: Workspace, root: Path) -> None:
    write(root / ".env", "needle=synthetic")
    write(root / "key.pem", "needle")
    write(root / ".env.example", "needle=placeholder")

    result = search_files(workspace, "needle")

    assert [m.path for m in result.matches] == [".env.example"]
    assert result.skipped.sensitive_files == 2


def test_search_skips_binary_and_invalid_utf8(workspace: Workspace, root: Path) -> None:
    write(root / "blob.bin", b"needle\x00\x01\x02")
    write(root / "latin1.txt", b"needle caf\xe9")
    write(root / "good.txt", "needle")

    result = search_files(workspace, "needle")

    assert [m.path for m in result.matches] == ["good.txt"]
    assert result.skipped.binary == 1
    assert result.skipped.not_utf8 == 1


def test_search_skips_oversized_files(workspace: Workspace, root: Path) -> None:
    write(root / "big.txt", "needle" + "x" * 100)
    write(root / "small.txt", "needle")

    result = search_files(workspace, "needle", limits=FilesystemLimits(max_search_file_bytes=50))

    assert [m.path for m in result.matches] == ["small.txt"]
    assert result.skipped.too_large == 1


def test_search_does_not_follow_links(workspace: Workspace, root: Path, outside: Path) -> None:
    link_directory(root / "escape", outside)
    write(root / "inside.txt", "nothing")

    result = search_files(workspace, OUTSIDE_MARKER)

    assert result.matches == []
    assert result.skipped.symlinks == 1


def test_search_skips_file_symlinks(workspace: Workspace, root: Path, outside: Path) -> None:
    link_file(root / "secret.txt", outside / "secret.txt")

    result = search_files(workspace, OUTSIDE_MARKER)

    assert result.matches == []
    assert result.skipped.symlinks == 1


def test_search_truncates_at_max_results(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "needle\n" * 5)

    result = search_files(workspace, "needle", max_results=3)

    assert [m.line for m in result.matches] == [1, 2, 3]
    assert result.truncated is True
    assert result.truncation_reason == "max_results"


def test_search_exact_fit_is_not_truncated(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "needle\n" * 3)

    result = search_files(workspace, "needle", max_results=3)

    assert len(result.matches) == 3
    assert result.truncated is False


def test_search_max_results_is_capped_by_limits(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "needle\n" * 5)

    result = search_files(
        workspace, "needle", max_results=500, limits=FilesystemLimits(max_search_results=2)
    )

    assert len(result.matches) == 2
    assert result.truncation_reason == "max_results"


def test_search_stops_at_max_files_scanned(workspace: Workspace, root: Path) -> None:
    for name in ["a.txt", "b.txt", "c.txt"]:
        write(root / name, "needle")

    result = search_files(workspace, "needle", limits=FilesystemLimits(max_files_scanned=2))

    assert [m.path for m in result.matches] == ["a.txt", "b.txt"]
    assert result.files_scanned == 2
    assert result.truncated is True
    assert result.truncation_reason == "max_files_scanned"


def test_search_stops_at_time_limit(workspace: Workspace, root: Path) -> None:
    for name in ["a.txt", "b.txt", "c.txt"]:
        write(root / name, "needle")
    # Deadline is 1.0. a.txt (at 0.0) and b.txt (at 0.5) are searched; c.txt (at 2.0) is not.
    ticks = iter([0.0, 0.0, 0.5, 2.0])

    result = search_files(
        workspace,
        "needle",
        limits=FilesystemLimits(search_timeout_seconds=1.0),
        clock=lambda: next(ticks),
    )

    assert [m.path for m in result.matches] == ["a.txt", "b.txt"]
    assert result.truncated is True
    assert result.truncation_reason == "time_limit"


def test_search_shortens_long_lines(workspace: Workspace, root: Path) -> None:
    write(root / "long.txt", "a" * 500 + "needle" + "b" * 500)

    (match,) = search_files(workspace, "needle").matches

    assert match.column == 501
    assert "needle" in match.snippet
    assert match.snippet.startswith("…")
    assert match.snippet.endswith("…")
    assert len(match.snippet) == 202


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("", "pattern must not be empty"),
        ("x" * 201, "pattern is too long: 201 characters; the limit is 200"),
        ("two\nlines", "pattern must be a single line"),
    ],
)
def test_search_rejects_bad_patterns(workspace: Workspace, pattern: str, message: str) -> None:
    with pytest.raises(WorkspaceError, match=message):
        search_files(workspace, pattern)


def test_search_rejects_non_positive_max_results(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="max_results must be at least 1"):
        search_files(workspace, "x", max_results=0)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("missing", "not found: 'missing'"),
        ("file.txt", "not a directory: 'file.txt'"),
        ("..", "path must not contain '..' components"),
        ("/", "path must be relative"),
    ],
)
def test_search_rejects_bad_paths(
    workspace: Workspace, root: Path, path: str, message: str, tmp_path: Path
) -> None:
    write(root / "file.txt", "x")

    with pytest.raises(WorkspaceError) as excinfo:
        search_files(workspace, "x", path)

    assert message in refusal(excinfo, tmp_path=tmp_path)


def test_search_counts_unreadable_files(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "locked.txt", "needle")
    write(root / "open.txt", "needle")
    real_open = workspace_module._open_file

    def deny(directory: workspace_module._Directory, name: str) -> int:
        if name == "locked.txt":
            raise PermissionError(13, "Permission denied", str(root / name))
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", deny)

    result = search_files(workspace, "needle")

    assert [m.path for m in result.matches] == ["open.txt"]
    assert result.skipped.unreadable_files == 1


# write_file

MIB = 1024 * 1024
BOM = b"\xef\xbb\xbf"


def after_free_check(
    monkeypatch: pytest.MonkeyPatch, name: str, action: Callable[[], None]
) -> None:
    """Run ``action`` right after write_file has seen that ``name`` does not exist yet.

    This is a real race: another process acting between the checks and the creation.
    """
    real_create = workspace_module._create_new_file

    def create(directory: workspace_module._Directory, created: str) -> object:
        if created == name:
            action()
        return real_create(directory, created)

    monkeypatch.setattr(workspace_module, "_create_new_file", create)


def test_write_file_creates_new_file(workspace: Workspace, root: Path) -> None:
    (root / "notes").mkdir()

    result = write_file(workspace, "notes/new.md", "# Title\n\nbody\n")

    assert result.path == "notes/new.md"
    assert result.bytes_written == 14
    assert (root / "notes" / "new.md").read_bytes() == b"# Title\n\nbody\n"


@pytest.mark.parametrize(
    "content",
    [
        "unix\nnewlines\n",
        "windows\r\nnewlines\r\n",
        "mixed\r\nold mac\rend\n",
        "no trailing newline",
        "héllo wörld 世界 🎉\n",
        "\t  indented  \n\n\n",
    ],
)
def test_write_file_preserves_content_exactly_without_bom(
    workspace: Workspace, root: Path, content: str
) -> None:
    result = write_file(workspace, "new.txt", content)

    data = (root / "new.txt").read_bytes()
    assert data == content.encode("utf-8")
    assert not data.startswith(BOM)
    assert result.bytes_written == len(data)


def test_write_file_counts_utf8_bytes_not_characters(workspace: Workspace, root: Path) -> None:
    # 1 + 2 + 3 + 4 bytes.
    result = write_file(workspace, "multibyte.txt", "aé世🎉")

    assert result.bytes_written == 10
    assert (root / "multibyte.txt").stat().st_size == 10


def test_write_file_keeps_a_bom_the_caller_supplied(workspace: Workspace, root: Path) -> None:
    result = write_file(workspace, "bom.txt", "\ufeffkept")

    assert (root / "bom.txt").read_bytes() == BOM + b"kept"
    assert result.bytes_written == 7


def test_write_empty_file(workspace: Workspace, root: Path) -> None:
    result = write_file(workspace, "empty.txt", "")

    assert result.bytes_written == 0
    assert (root / "empty.txt").read_bytes() == b""


def test_write_file_round_trips_through_read_file(workspace: Workspace, root: Path) -> None:
    (root / "sub").mkdir()

    created = write_file(workspace, "sub\\round.txt", "line 1\r\nline 2 ✓\n")
    read = read_file(workspace, created.path)

    assert created.path == "sub/round.txt"
    assert read.content == "line 1\r\nline 2 ✓\n"
    assert read.size_bytes == created.bytes_written


def test_write_env_template_is_allowed(workspace: Workspace, root: Path) -> None:
    write_file(workspace, ".env.example", "KEY=placeholder\n")

    assert (root / ".env.example").read_bytes() == b"KEY=placeholder\n"


@pytest.mark.parametrize(
    "path",
    [".github/workflows/ci.yml", "src/app.py", "docs/guide.md", "gitlab-notes/todo.txt"],
)
def test_write_allows_normal_project_directories(
    workspace: Workspace, root: Path, path: str
) -> None:
    (root / path).parent.mkdir(parents=True)

    assert write_file(workspace, path, "ok").path == path
    assert (root / path).read_bytes() == b"ok"


# write_file: existing targets are never replaced


def test_write_refuses_to_overwrite_existing_file(
    workspace: Workspace, root: Path, tmp_path: Path
) -> None:
    target = write(root / "a.txt", "original contents\n")
    before = target.stat()

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, "a.txt", "replacement")

    assert refusal(excinfo, tmp_path=tmp_path) == "already exists: 'a.txt'"
    assert target.read_bytes() == b"original contents\n"
    assert target.stat().st_mtime_ns == before.st_mtime_ns


def test_write_refuses_existing_file_with_different_case(workspace: Workspace, root: Path) -> None:
    target = write(root / "Readme.md", "original")
    if not (root / "README.MD").exists():
        pytest.skip("case-sensitive filesystem")

    with pytest.raises(WorkspaceError, match="already exists"):
        write_file(workspace, "README.MD", "replacement")

    assert target.read_bytes() == b"original"


def test_write_refuses_existing_directory(workspace: Workspace, root: Path) -> None:
    write(root / "sub" / "keep.txt", "keep")

    with pytest.raises(WorkspaceError, match=re.escape("already exists: 'sub'")):
        write_file(workspace, "sub", "x")

    assert (root / "sub" / "keep.txt").read_bytes() == b"keep"


def test_write_refuses_existing_directory_link(
    workspace: Workspace, root: Path, outside: Path, tmp_path: Path
) -> None:
    link_directory(root / "escape", outside)

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, "escape", "x")

    assert refusal(excinfo, tmp_path=tmp_path) == "already exists: 'escape'"
    assert sorted(os.listdir(outside)) == ["secret.txt"]


def test_write_refuses_existing_file_symlink(
    workspace: Workspace, root: Path, outside: Path
) -> None:
    link_file(root / "link.txt", outside / "secret.txt")

    with pytest.raises(WorkspaceError, match=re.escape("already exists: 'link.txt'")):
        write_file(workspace, "link.txt", "x")

    assert (outside / "secret.txt").read_text(encoding="utf-8") == f"{OUTSIDE_MARKER}\n"


def test_write_refuses_dangling_symlink(workspace: Workspace, root: Path, outside: Path) -> None:
    link_file(root / "dangling.txt", outside / "created-through-link.txt")

    with pytest.raises(WorkspaceError, match=re.escape("already exists: 'dangling.txt'")):
        write_file(workspace, "dangling.txt", "x")

    assert not (outside / "created-through-link.txt").exists()


@pytest.mark.parametrize("kind", ["file", "directory", "directory link", "file symlink"])
def test_write_target_created_concurrently_is_not_replaced(
    workspace: Workspace,
    root: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    """The exclusive create itself refuses a target that appears after the checks."""
    target = root / "new.txt"
    if kind == "file symlink":
        # Probe first, so an unprivileged Windows run skips before patching anything.
        link_file(tmp_path / "probe", outside / "secret.txt")

    def appear() -> None:
        if kind == "file":
            write(target, "competitor")
        elif kind == "directory":
            target.mkdir()
        elif kind == "directory link":
            link_directory(target, outside)
        else:
            target.symlink_to(outside / "secret.txt")

    after_free_check(monkeypatch, "new.txt", appear)

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, "new.txt", "mine")

    assert refusal(excinfo, tmp_path=tmp_path) == "already exists: 'new.txt'"
    if kind == "file":
        assert target.read_bytes() == b"competitor"
    assert (outside / "secret.txt").read_text(encoding="utf-8") == f"{OUTSIDE_MARKER}\n"
    assert sorted(os.listdir(outside)) == ["secret.txt"]


# write_file: failed writes leave nothing behind


def test_write_failure_removes_incomplete_file(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_halfway(fd: int, data: bytes) -> None:
        os.write(fd, data[: len(data) // 2])
        raise OSError(errno.ENOSPC, "No space left on device", str(tmp_path / "x"))

    monkeypatch.setattr(workspace_module, "_write_all", fail_halfway)

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, "new.txt", "0123456789")

    assert refusal(excinfo, tmp_path=tmp_path) == (
        "failed to write 'new.txt'; the new file was removed"
    )
    assert os.listdir(root) == []


def test_write_flush_failure_removes_incomplete_file(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(fd: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "fsync", fail)

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, "new.txt", "data")

    assert refusal(excinfo, tmp_path=tmp_path) == (
        "failed to write 'new.txt'; the new file was removed"
    )
    assert os.listdir(root) == []


def test_write_interrupted_removes_incomplete_file_and_reraises(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(fd: int, data: bytes) -> None:
        os.write(fd, data[:2])
        raise KeyboardInterrupt

    monkeypatch.setattr(workspace_module, "_write_all", interrupt)

    with pytest.raises(KeyboardInterrupt):
        write_file(workspace, "new.txt", "data")

    assert os.listdir(root) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows locks the new file; see next test")
def test_write_cleanup_never_removes_a_file_that_replaced_ours(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def replace_then_fail(fd: int, data: bytes) -> None:
        os.write(fd, data[:2])
        os.rename(root / "new.txt", root / "moved.txt")
        write(root / "new.txt", "someone else's file")
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(workspace_module, "_write_all", replace_then_fail)

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, "new.txt", "data")

    assert refusal(excinfo, tmp_path=tmp_path) == (
        "failed to write 'new.txt'; the incomplete file could not be removed safely"
    )
    assert (root / "new.txt").read_bytes() == b"someone else's file"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes")
def test_write_new_file_is_locked_while_being_written(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused: list[str] = []
    attempts: list[tuple[str, Callable[[], object]]] = [
        ("rename", lambda: os.rename(root / "new.txt", root / "moved.txt")),
        ("delete", lambda: os.unlink(root / "new.txt")),
        ("open", lambda: (root / "new.txt").read_bytes()),
    ]

    def tamper_then_fail(fd: int, data: bytes) -> None:
        os.write(fd, data[:2])
        for name, attempt in attempts:
            try:
                attempt()
            except PermissionError:
                refused.append(name)
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(workspace_module, "_write_all", tamper_then_fail)

    with pytest.raises(WorkspaceError, match="the new file was removed"):
        write_file(workspace, "new.txt", "data")

    assert refused == ["rename", "delete", "open"]
    assert os.listdir(root) == []


@pytest.mark.parametrize("swap_to", ["outside", "other", ".git"])
def test_write_parent_swapped_for_link_after_checks_is_refused(
    workspace: Workspace,
    root: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    swap_to: str,
) -> None:
    """A real race: a checked parent directory is replaced by a link before the create."""
    (root / "sub").mkdir()
    (root / "other").mkdir()
    (root / ".git").mkdir()
    destination = outside if swap_to == "outside" else root / swap_to
    blocked: list[str] = []

    def swap() -> None:
        try:
            (root / "sub").rename(root / "sub-real")
        except PermissionError:
            blocked.append("sub")
            return
        link_directory(root / "sub", destination)

    after_free_check(monkeypatch, "new.txt", swap)

    if sys.platform == "win32":
        # The parent is held open without delete sharing, so the swap itself cannot happen.
        write_file(workspace, "sub/new.txt", "data")
        assert blocked == ["sub"]
        assert (root / "sub" / "new.txt").read_bytes() == b"data"
    else:
        with pytest.raises(WorkspaceError) as excinfo:
            write_file(workspace, "sub/new.txt", "data")

        # POSIX creates through the pinned descriptor, then sees where the file ended up.
        assert refusal(excinfo, tmp_path=tmp_path) == (
            "directory changed while the file was being created: 'sub/new.txt'; "
            "the new file was removed"
        )
        assert not (root / "sub-real" / "new.txt").exists()
    for directory in (outside, root / "other", root / ".git"):
        assert not (directory / "new.txt").exists()


def test_write_permission_error_is_reported_safely(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def deny(*_args: object) -> object:
        raise PermissionError(13, "Permission denied", str(root / "new.txt"))

    monkeypatch.setattr(workspace_module, "_create_new_file", deny)

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, "new.txt", "x")

    assert refusal(excinfo, tmp_path=tmp_path) == "permission denied: 'new.txt'"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_write_into_read_only_directory_posix(
    workspace: Workspace, root: Path, tmp_path: Path
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses permission bits")
    locked = root / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        with pytest.raises(WorkspaceError) as excinfo:
            write_file(workspace, "locked/new.txt", "x")
    finally:
        locked.chmod(0o755)

    assert refusal(excinfo, tmp_path=tmp_path) == "permission denied: 'locked/new.txt'"
    assert os.listdir(locked) == []


# write_file: path validation and confinement


@pytest.mark.parametrize(
    "path",
    [
        "/etc/new.txt",
        "\\Windows\\new.txt",
        "C:\\Windows\\new.txt",
        "C:/new.txt",
        "C:new.txt",
        "\\\\server\\share\\new.txt",
        "//server/share/new.txt",
        "\\\\?\\C:\\new.txt",
    ],
)
def test_write_absolute_paths_are_refused(workspace: Workspace, path: str, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, path, "x")

    assert "path must be relative" in refusal(excinfo, tmp_path=tmp_path)


def test_write_absolute_path_outside_workspace_is_refused(
    workspace: Workspace, outside: Path, tmp_path: Path
) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, str(outside / "new.txt"), "x")

    assert "path must be relative" in refusal(excinfo, tmp_path=tmp_path)
    assert not (outside / "new.txt").exists()


@pytest.mark.parametrize(
    "path",
    ["../new.txt", "..\\outside\\new.txt", "sub/../../outside/new.txt", "sub/../new.txt"],
)
def test_write_traversal_is_refused(
    workspace: Workspace, root: Path, outside: Path, path: str, tmp_path: Path
) -> None:
    (root / "sub").mkdir()

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, path, "x")

    assert refusal(excinfo, tmp_path=tmp_path) == "path must not contain '..' components"
    assert sorted(os.listdir(outside)) == ["secret.txt"]
    assert os.listdir(root) == ["sub"]


def test_write_nul_in_path_is_refused(workspace: Workspace, root: Path) -> None:
    with pytest.raises(WorkspaceError, match="NUL"):
        write_file(workspace, "a.txt\x00.png", "x")

    assert os.listdir(root) == []


@pytest.mark.parametrize("path", ["", ".", "./", "sub/", "sub\\", "sub/.", "new.txt/"])
def test_write_requires_a_file_name(workspace: Workspace, root: Path, path: str) -> None:
    (root / "sub").mkdir()

    with pytest.raises(WorkspaceError, match="path must name a file, not a directory"):
        write_file(workspace, path, "x")

    assert os.listdir(root) == ["sub"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path rules")
@pytest.mark.parametrize("path", ["NUL", "con.txt", "sub/COM1", "a.txt:stream", "a.txt.", "a.txt "])
def test_write_windows_reserved_names_are_refused(
    workspace: Workspace, root: Path, path: str
) -> None:
    (root / "sub").mkdir()

    with pytest.raises(WorkspaceError, match="path component is not allowed on Windows"):
        write_file(workspace, path, "x")

    assert os.listdir(root) == ["sub"]


@pytest.mark.parametrize(
    ("path", "missing"),
    [("missing/new.txt", "missing"), ("a/b/new.txt", "a/b"), ("a/b/c/new.txt", "a/b/c")],
)
def test_write_requires_existing_parent(
    workspace: Workspace, root: Path, path: str, missing: str, tmp_path: Path
) -> None:
    (root / "a").mkdir()

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, path, "x")

    assert refusal(excinfo, tmp_path=tmp_path) == f"parent directory does not exist: '{missing}'"
    assert os.listdir(root) == ["a"]
    assert os.listdir(root / "a") == []


def test_write_parent_that_is_a_file_is_refused(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "a")

    with pytest.raises(WorkspaceError, match=re.escape("not a directory: 'a.txt'")):
        write_file(workspace, "a.txt/new.txt", "x")

    assert (root / "a.txt").read_bytes() == b"a"


def test_write_through_directory_link_outside_is_refused(
    workspace: Workspace, root: Path, outside: Path, tmp_path: Path
) -> None:
    link_directory(root / "escape", outside)

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, "escape/new.txt", "x")

    assert refusal(excinfo, tmp_path=tmp_path) == (
        "refusing to follow symlink or junction: 'escape'"
    )
    assert sorted(os.listdir(outside)) == ["secret.txt"]


def test_write_through_nested_directory_link_is_refused(
    workspace: Workspace, root: Path, tmp_path: Path
) -> None:
    (root / "real").mkdir()
    (root / "a").mkdir()
    link_directory(root / "a" / "alias", root / "real")

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, "a/alias/new.txt", "x")

    assert refusal(excinfo, tmp_path=tmp_path) == (
        "refusing to follow symlink or junction: 'a/alias'"
    )
    assert os.listdir(root / "real") == []


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".ENV",
        ".env.local",
        "config/.env.production",
        ".netrc",
        "id_rsa",
        "deploy/id_ed25519",
        "server.pem",
        "tls.KEY",
        "cert.p12",
    ],
)
def test_write_sensitive_files_are_refused(
    workspace: Workspace, root: Path, path: str, tmp_path: Path
) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, path, "API_KEY=synthetic-not-a-secret")

    assert "writing sensitive files is not allowed" in refusal(excinfo, tmp_path=tmp_path)
    assert not (root / path).exists()


def test_write_sensitive_refusal_does_not_reveal_parent_existence(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="writing sensitive files is not allowed"):
        write_file(workspace, "missing/.env", "x")


@pytest.mark.parametrize(
    "path", [".ssh/config", "nested/.SSH/known_hosts", ".aws/credentials", ".gnupg/gpg.conf"]
)
def test_write_sensitive_directories_are_refused(
    workspace: Workspace, root: Path, path: str
) -> None:
    (root / path).parent.mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="access to sensitive directories is not allowed"):
        write_file(workspace, path, "synthetic")

    assert not (root / path).exists()


@pytest.mark.parametrize(
    "path",
    [
        ".venv/lib/sitecustomize.py",
        "venv/pyvenv.cfg.new",
        "sub/node_modules/pkg/index.js",
        "__pycache__/mod.cpython-313.pyc",
        "pkg.egg-info/PKG-INFO.new",
        ".pytest_cache/state",
        ".mypy_cache/state",
    ],
)
def test_write_inside_excluded_directories_is_refused(
    workspace: Workspace, root: Path, path: str, tmp_path: Path
) -> None:
    (root / path).parent.mkdir(parents=True)

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, path, "x")

    assert refusal(excinfo, tmp_path=tmp_path) == (
        f"writing to excluded directories is not allowed: '{path}'"
    )
    assert not (root / path).exists()


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (".git", "access to repository metadata is not allowed"),
        ("sub/.git", "access to repository metadata is not allowed"),
        ("node_modules", "writing to excluded directories is not allowed"),
        (".venv", "writing to excluded directories is not allowed"),
    ],
)
def test_write_file_named_like_excluded_directory_is_refused(
    workspace: Workspace, root: Path, path: str, message: str
) -> None:
    """A '.git' file can point Git at another repository, so it is refused too."""
    (root / "sub").mkdir()

    with pytest.raises(WorkspaceError, match=message):
        write_file(workspace, path, "gitdir: ../elsewhere\n")

    assert not (root / path).exists()


@pytest.mark.parametrize(
    ("directory", "message"),
    [
        (".git", "access to repository metadata is not allowed"),
        (".ssh", "access to sensitive directories is not allowed"),
        ("node_modules", "writing to excluded directories is not allowed"),
    ],
)
def test_write_through_short_name_alias_is_refused(
    workspace: Workspace, root: Path, directory: str, message: str
) -> None:
    (root / directory).mkdir()
    alias = short_name(root / directory)

    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, f"{alias}/new.txt", "x")

    # Refused by the parent's real-path check, before anything is created.
    assert str(excinfo.value) == f"{message}: '{alias}/new.txt'"
    assert os.listdir(root / directory) == []


# write_file: content validation


def test_write_nul_in_content_is_refused(workspace: Workspace, root: Path, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        write_file(workspace, "new.txt", "text\x00more")

    assert refusal(excinfo, tmp_path=tmp_path) == "content must not contain NUL characters"
    assert os.listdir(root) == []


def test_write_unpaired_surrogate_is_refused(workspace: Workspace, root: Path) -> None:
    with pytest.raises(
        WorkspaceError,
        match=re.escape("content is not valid Unicode text (unpaired surrogate at character 2)"),
    ):
        write_file(workspace, "new.txt", "ok\ud800")

    assert os.listdir(root) == []


@pytest.mark.parametrize(
    "content",
    ["x" * MIB, "€" * 349_525 + "a"],
    ids=["ascii", "multibyte"],
)
def test_write_content_at_exact_limit_is_accepted(
    workspace: Workspace, root: Path, content: str
) -> None:
    result = write_file(workspace, "big.txt", content)

    assert result.bytes_written == MIB
    assert (root / "big.txt").read_bytes() == content.encode("utf-8")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("x" * (MIB + 1), "content is too large: more than 1048576 bytes as UTF-8"),
        # Fewer characters than the limit, but 3 bytes each once encoded.
        ("€" * 349_526, "content is too large: 1048578 bytes as UTF-8"),
        ("x" * (MIB - 1) + "é", "content is too large: 1048577 bytes as UTF-8"),
    ],
    ids=["ascii", "multibyte", "multibyte-boundary"],
)
def test_write_content_over_limit_is_refused(
    workspace: Workspace, root: Path, content: str, message: str
) -> None:
    with pytest.raises(WorkspaceError, match=re.escape(message)):
        write_file(workspace, "big.txt", content)

    assert os.listdir(root) == []


def test_write_respects_configured_limit(workspace: Workspace, root: Path) -> None:
    limits = FilesystemLimits(max_write_bytes=4)

    assert write_file(workspace, "fits.txt", "abcd", limits=limits).bytes_written == 4
    with pytest.raises(WorkspaceError, match="content is too large: 6 bytes as UTF-8"):
        write_file(workspace, "big.txt", "ééé", limits=limits)
    assert sorted(os.listdir(root)) == ["fits.txt"]

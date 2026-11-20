"""edit_file, create_directory, and move_path: what they change, what they refuse, and proof."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from support import link_directory, link_file, short_name, write
from universal_ai_tools.file_actions import (
    CreateDirectoryInput,
    EditFileInput,
    MovePathInput,
    create_directory,
    edit_file,
    move_path,
)
from universal_ai_tools.filesystem import FilesystemLimits
from universal_ai_tools.workspace import Workspace, WorkspaceError

LIMITS = FilesystemLimits(max_read_bytes=4096, max_write_bytes=4096)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = (tmp_path / "workspace").resolve()
    write(path / "docs" / "a.txt", "one two two\n")
    write(path / "docs" / "crlf.txt", b"first\r\nsecond\r\n")
    write(path / ".env", "KEY=1\n")
    write(path / ".git" / "config", "[core]\n")
    write(path / ".venv" / "lib.py", "x = 1\n")
    return path


@pytest.fixture
def ws(root: Path) -> Workspace:
    return Workspace(root)


def edit(ws: Workspace, path: str, old: str, new: str, times: int = 1) -> object:
    return edit_file(
        ws,
        EditFileInput(path=path, old_text=old, new_text=new, expected_replacements=times),
        LIMITS,
    )


def leftovers(root: Path) -> list[str]:
    return [p.name for p in root.rglob("*.tmp")]


# edit_file


def test_edit_replaces_and_verifies(ws: Workspace, root: Path) -> None:
    result = edit(ws, "docs/a.txt", "one", "ONE")

    assert (root / "docs" / "a.txt").read_bytes() == b"ONE two two\n"
    assert result.verified is True  # type: ignore[attr-defined]
    assert result.replacements == 1  # type: ignore[attr-defined]
    assert leftovers(root) == []


def test_edit_requires_the_exact_count(ws: Workspace, root: Path) -> None:
    with pytest.raises(WorkspaceError, match="occurs 2 time"):
        edit(ws, "docs/a.txt", "two", "2")
    assert (root / "docs" / "a.txt").read_bytes() == b"one two two\n"

    edit(ws, "docs/a.txt", "two", "2", times=2)
    assert (root / "docs" / "a.txt").read_bytes() == b"one 2 2\n"


def test_edit_matches_lf_against_crlf_and_keeps_crlf(ws: Workspace, root: Path) -> None:
    edit(ws, "docs/crlf.txt", "first\nsecond", "1st\n2nd")

    assert (root / "docs" / "crlf.txt").read_bytes() == b"1st\r\n2nd\r\n"


def test_missing_text_changes_nothing(ws: Workspace, root: Path) -> None:
    with pytest.raises(WorkspaceError, match="occurs 0 time"):
        edit(ws, "docs/a.txt", "absent", "x")
    assert (root / "docs" / "a.txt").read_bytes() == b"one two two\n"
    assert leftovers(root) == []


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (".env", "modifying sensitive files is not allowed"),
        (".git/config", "repository metadata"),
        (".venv/lib.py", "excluded directories"),
        ("../outside.txt", "'..'"),
        ("docs", "is a directory"),
        ("docs/missing.txt", "not found"),
    ],
)
def test_edit_refusals(ws: Workspace, path: str, message: str) -> None:
    with pytest.raises(WorkspaceError, match=message):
        edit(ws, path, "x", "y")


def test_edit_refuses_binary_and_oversized_results(ws: Workspace, root: Path) -> None:
    write(root / "bin.dat", b"a\x00b")
    with pytest.raises(WorkspaceError, match="NUL"):
        edit(ws, "bin.dat", "a", "b")
    with pytest.raises(WorkspaceError, match="limit"):
        edit(ws, "docs/a.txt", "one", "x" * 5000)
    assert (root / "docs" / "a.txt").read_bytes() == b"one two two\n"


def test_edit_refuses_a_hard_linked_file(ws: Workspace, root: Path, tmp_path: Path) -> None:
    outside = write(tmp_path / "outside.txt", "secret one\n")
    os.link(outside, root / "docs" / "linked.txt")

    with pytest.raises(WorkspaceError, match="hard-linked"):
        edit(ws, "docs/linked.txt", "one", "two")
    assert outside.read_bytes() == b"secret one\n"


def test_edit_refuses_symlinks(ws: Workspace, root: Path, tmp_path: Path) -> None:
    outside = write(tmp_path / "outside.txt", "secret one\n")
    link_file(root / "docs" / "link.txt", outside)

    with pytest.raises(WorkspaceError, match="symlink"):
        edit(ws, "docs/link.txt", "one", "two")
    assert outside.read_bytes() == b"secret one\n"


def test_edit_refuses_a_path_through_a_junction(ws: Workspace, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside / "victim.txt", "secret one\n")
    link_directory(root / "jump", outside)

    with pytest.raises(WorkspaceError, match="symlink or junction"):
        edit(ws, "jump/victim.txt", "one", "two")
    assert (outside / "victim.txt").read_bytes() == b"secret one\n"


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names are a Windows feature")
def test_edit_refuses_an_8dot3_alias_of_a_sensitive_file(ws: Workspace, root: Path) -> None:
    write(root / ".env.production", "KEY=1\n")
    alias = short_name(root / ".env.production")

    with pytest.raises(WorkspaceError, match="sensitive"):
        edit(ws, alias, "KEY", "X")


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names are a Windows feature")
def test_edit_refuses_an_excluded_directory_reached_by_its_8dot3_alias(
    ws: Workspace, root: Path
) -> None:
    """Reading .venv is allowed; changing it is not, even under its short name."""
    alias = short_name(root / ".venv")

    with pytest.raises(WorkspaceError, match="excluded directories"):
        edit(ws, f"{alias}/lib.py", "x", "y")
    assert (root / ".venv" / "lib.py").read_bytes() == b"x = 1\n"


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names are a Windows feature")
@pytest.mark.parametrize("directory", [".venv", "node_modules"])
def test_an_edit_refused_on_the_real_path_creates_nothing_first(
    ws: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    """The MODIFY policy is applied to the real path of the file being read, before the new
    content's temporary file exists. A later check on that temporary file would also refuse,
    but only after creating it inside the directory nothing may be written to."""
    from universal_ai_tools import workspace as workspace_module
    from universal_ai_tools.workspace import PolicyRefusedError

    write(root / directory / "x.txt", "old\n")
    before = sorted(p.name for p in (root / directory).iterdir())
    alias = short_name(root / directory)
    created: list[str] = []
    real_create = workspace_module._create_new_file

    def recording_create(parent: object, name: str) -> object:
        created.append(name)
        return real_create(parent, name)  # type: ignore[arg-type]

    monkeypatch.setattr(workspace_module, "_create_new_file", recording_create)
    assert ws.read_bytes(ws.resolve(f"{alias}/x.txt"), 4096) == b"old\n"  # reading is allowed

    with pytest.raises(PolicyRefusedError, match="excluded directories"):
        edit(ws, f"{alias}/x.txt", "old", "new")

    assert created == []
    assert sorted(p.name for p in (root / directory).iterdir()) == before
    assert (root / directory / "x.txt").read_bytes() == b"old\n"


# create_directory


def test_create_directory(ws: Workspace, root: Path) -> None:
    result = create_directory(ws, CreateDirectoryInput(path="docs/new"))

    assert (root / "docs" / "new").is_dir()
    assert result.verified is True


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("docs", "already exists"),
        ("docs/a.txt", "already exists"),
        ("missing/new", "parent directory does not exist"),
        (".git/hooks2", "repository metadata"),
        (".venv/new", "excluded directories"),
        (".ssh", "sensitive directories"),
        (".", "workspace root"),
    ],
)
def test_create_directory_refusals(ws: Workspace, path: str, message: str) -> None:
    with pytest.raises(WorkspaceError, match=message):
        create_directory(ws, CreateDirectoryInput(path=path))


def test_create_directory_never_through_a_junction(
    ws: Workspace, root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link_directory(root / "jump", outside)

    with pytest.raises(WorkspaceError, match="symlink or junction"):
        create_directory(ws, CreateDirectoryInput(path="jump/new"))
    assert list(outside.iterdir()) == []


# move_path


def test_move_file_and_directory(ws: Workspace, root: Path) -> None:
    moved = move_path(ws, MovePathInput(source="docs/a.txt", destination="a.txt"))
    assert moved.verified is True
    assert moved.type == "file"
    assert (root / "a.txt").read_bytes() == b"one two two\n"
    assert not (root / "docs" / "a.txt").exists()

    moved = move_path(ws, MovePathInput(source="docs", destination="archive"))
    assert moved.verified is True
    assert moved.type == "directory"
    assert (root / "archive" / "crlf.txt").exists()


@pytest.mark.parametrize(
    ("source", "destination", "message"),
    [
        ("docs/a.txt", "docs/crlf.txt", "already exists"),
        ("docs/a.txt", "docs", "already exists"),
        ("docs", "docs/inner", "into itself"),
        ("docs/a.txt", "missing/a.txt", "destination directory does not exist"),
        ("docs/missing.txt", "x.txt", "not found"),
        (".env", "env.txt", "modifying sensitive files"),
        ("docs/a.txt", ".env", "writing sensitive files"),
        (".git", "git", "repository metadata"),
        ("docs/a.txt", ".git/a.txt", "repository metadata"),
        ("docs/a.txt", ".venv/a.txt", "excluded directories"),
        (".venv", "venv-copy", "excluded directories"),
        ("docs/a.txt", "../a.txt", "'..'"),
        (".", "x", "workspace root"),
    ],
)
def test_move_refusals(
    ws: Workspace, root: Path, source: str, destination: str, message: str
) -> None:
    before = sorted(str(p) for p in root.rglob("*"))
    with pytest.raises(WorkspaceError, match=message):
        move_path(ws, MovePathInput(source=source, destination=destination))
    assert sorted(str(p) for p in root.rglob("*")) == before


def test_move_never_out_through_a_junction(ws: Workspace, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link_directory(root / "jump", outside)

    with pytest.raises(WorkspaceError, match="symlink or junction"):
        move_path(ws, MovePathInput(source="docs/a.txt", destination="jump/a.txt"))
    with pytest.raises(WorkspaceError, match="refusing to move a symlink or junction"):
        move_path(ws, MovePathInput(source="jump", destination="elsewhere"))
    assert list(outside.iterdir()) == []
    assert (root / "docs" / "a.txt").exists()
    # The junction itself was not moved either.
    assert (root / "jump").exists()
    assert not (root / "elsewhere").exists()

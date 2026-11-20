"""Regressions for the findings of the cloud review of 2026-11-10, one test (or more) each."""

from __future__ import annotations

import ast
import os
import stat
import sys
from pathlib import Path

import pytest

import universal_ai_tools.workspace as workspace_module
from universal_ai_tools.workspace import PolicyRefusedError, Workspace, WorkspaceError


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def upper(data: bytes) -> bytes:
    return data.upper()


# Editing files named env.py, env.example, env-config.js, env_settings.py.


@pytest.mark.parametrize("name", ["env.py", "env.example", "env-config.js", "env_settings.py"])
def test_files_named_env_something_can_be_edited(root: Path, name: str) -> None:
    folder = root / "migrations"
    folder.mkdir()
    (folder / name).write_bytes(b"value = 1\n")
    old, new = Workspace(root).replace_file(f"migrations/{name}", upper, max_read_bytes=1000)
    assert (old, new) == (b"value = 1\n", b"VALUE = 1\n")
    assert (folder / name).read_bytes() == b"VALUE = 1\n"
    assert sorted(p.name for p in folder.iterdir()) == [name]  # no temporary file left


def test_real_secrets_files_are_still_refused(root: Path) -> None:
    (root / ".env").write_bytes(b"KEY=1\n")
    with pytest.raises(WorkspaceError):
        Workspace(root).replace_file(".env", upper, max_read_bytes=1000)


# An edit keeps the file's permissions.


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o755, 0o600, 0o640])
def test_an_edit_keeps_the_permissions(root: Path, mode: int) -> None:
    script = root / "build.sh"
    script.write_bytes(b"echo hi\n")
    script.chmod(mode)
    Workspace(root).replace_file("build.sh", upper, max_read_bytes=1000)
    assert stat.S_IMODE(script.stat().st_mode) == mode


# A change made in place while editing is not overwritten.


def test_a_change_in_place_during_the_edit_is_not_lost(root: Path) -> None:
    target = root / "notes.txt"
    target.write_bytes(b"one\n")

    def meanwhile(data: bytes) -> bytes:
        with open(target, "ab") as handle:  # same file, same inode
            handle.write(b"added by someone else\n")
        return data + b"two\n"

    with pytest.raises(WorkspaceError, match="changed while being edited"):
        Workspace(root).replace_file("notes.txt", meanwhile, max_read_bytes=1000)
    assert target.read_bytes() == b"one\nadded by someone else\n"
    assert [p.name for p in root.iterdir()] == ["notes.txt"]


# Moving a directory checks what it contains.


@pytest.mark.parametrize(
    "inner",
    [".env", "keys/id_rsa", "cert.pem", ".git/config", ".ssh/known_hosts", "node_modules/x"],
)
def test_a_directory_holding_refused_entries_is_not_moved(root: Path, inner: str) -> None:
    source = root / "config"
    (source / inner).parent.mkdir(parents=True, exist_ok=True)
    (source / inner).write_bytes(b"x")
    (root / "public").mkdir()
    with pytest.raises(PolicyRefusedError, match="contains"):
        Workspace(root).move("config", "public/config")
    assert (source / inner).exists()
    assert not (root / "public" / "config").exists()


def test_an_ordinary_directory_still_moves(root: Path) -> None:
    (root / "docs" / "sub").mkdir(parents=True)
    (root / "docs" / "sub" / "a.md").write_bytes(b"a")
    (root / "archive").mkdir()
    assert Workspace(root).move("docs", "archive/docs") == ("docs", "archive/docs", True)
    assert (root / "archive" / "docs" / "sub" / "a.md").read_bytes() == b"a"


def test_a_directory_too_large_to_check_is_not_moved(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, "MAX_MOVE_CHECKED_ENTRIES", 3)
    (root / "big").mkdir()
    for n in range(5):
        (root / "big" / f"{n}.txt").write_bytes(b"x")
    with pytest.raises(PolicyRefusedError, match="too many"):
        Workspace(root).move("big", "big2")
    assert (root / "big").is_dir()


# Renames that change only the case of a name.


def test_a_rename_that_changes_only_case(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "docs" / "readme.md").write_bytes(b"hello")
    Workspace(root).move("docs/readme.md", "docs/README.md")
    assert [p.name for p in (root / "docs").iterdir()] == ["README.md"]
    assert (root / "docs" / "README.md").read_bytes() == b"hello"


def test_a_different_existing_file_still_blocks_a_move(root: Path) -> None:
    (root / "a.txt").write_bytes(b"a")
    (root / "b.txt").write_bytes(b"b")
    with pytest.raises(WorkspaceError, match="already exists"):
        Workspace(root).move("a.txt", "b.txt")
    assert (root / "b.txt").read_bytes() == b"b"


def test_moving_into_itself_is_still_refused(root: Path) -> None:
    (root / "src").mkdir()
    with pytest.raises(WorkspaceError, match="into itself"):
        Workspace(root).move("src", "src/inner")


@pytest.mark.parametrize(
    ("candidate", "container", "inside"),
    [
        (["a", "b"], ["a"], True),
        (["a"], ["a"], False),  # the same path is not inside itself
        (["a", "b"], ["a", "b"], False),
        (["ab"], ["a"], False),
        (["b", "a"], ["a"], False),
    ],
)
def test_inside(candidate: list[str], container: list[str], inside: bool) -> None:
    assert workspace_module._inside(candidate, container) is inside


@pytest.mark.skipif(sys.platform == "win32", reason="Linux: names that differ only in case")
def test_case_sensitive_systems_compare_exactly() -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS volumes are usually case-insensitive")
    assert workspace_module._inside(["Build", "x"], ["build"]) is False


# Importing the module starts no process (ctypes.util.find_library runs ldconfig on Linux).


def test_the_workspace_module_never_calls_find_library() -> None:
    tree = ast.parse(Path(workspace_module.__file__).read_text(encoding="utf-8"))
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "find_library" not in names
    assert "ctypes.util" not in imports


def test_no_temporary_name_comes_from_the_file_name(root: Path) -> None:
    """The edit's temporary file must never take a name the policy refuses."""
    created: list[str] = []
    original = Workspace._create_in

    def spy(self: Workspace, parent: object, name: str, relative: str, data: bytes) -> None:
        created.append(name)
        original(self, parent, name, relative, data)  # type: ignore[arg-type]

    (root / "env.py").write_bytes(b"x")
    workspace = Workspace(root)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Workspace, "_create_in", spy)
        workspace.replace_file("env.py", upper, max_read_bytes=10)
    assert len(created) == 1
    assert created[0].startswith(".uat-edit-")
    assert "env" not in created[0]
    assert os.path.exists(root / "env.py")

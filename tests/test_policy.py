"""Tests for the shared filesystem policy, through the operations that apply it.

Each rule is exercised on a real filesystem through list_files, read_file, search_files,
and write_file, so a rule cannot hold for one tool and not another. Everything is
synthetic and lives under pytest's tmp_path.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from support import junction, link_directory, link_file, short_name, write
from universal_ai_tools import filesystem as filesystem_module
from universal_ai_tools import workspace as workspace_module
from universal_ai_tools.config import ENV_WORKSPACE, load_config
from universal_ai_tools.filesystem import (
    FilesystemLimits,
    ListFilesResult,
    SearchFilesResult,
    list_files,
    read_file,
    search_files,
    write_file,
)
from universal_ai_tools.policy import (
    Operation,
    escape_line,
    escape_text,
    is_excluded_directory_name,
    is_sensitive_file_name,
    refusal,
)
from universal_ai_tools.workspace import MAX_PATH_CHARS, HardLinkError, Workspace, WorkspaceError

MARKER = "SYNTHETIC-NEEDLE-CONTENT"

SENSITIVE_FILE_NAMES = [
    # Environment files.
    ".env",
    ".ENV",
    ".env.local",
    ".env.production",
    ".env.bak",
    ".env-prod",
    ".env_local",
    ".env~",
    ".envrc",
    "prod.env",
    "test.env",
    "PROD.ENV",
    # env.<stage>, every stage on the list.
    "env.ci",
    "env.dev",
    "env.development",
    "env.local",
    "env.preview",
    "env.prod",
    "env.production",
    "env.qa",
    "env.stage",
    "env.staging",
    "env.test",
    "env.testing",
    "env.uat",
    "ENV.PROD",
    # Credential files.
    ".netrc",
    "_netrc",
    ".pgpass",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".dockercfg",
    ".htpasswd",
    "credentials",
    "CREDENTIALS",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "secrets.toml",
    "Secrets.YAML",
    # Keys and stores.
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "server.pem",
    "tls.KEY",
    "cert.p12",
    "cert.pfx",
    "putty.ppk",
    "app.jks",
    "app.keystore",
    "vault.kdbx",
    "terraform.tfstate",
    "prod.tfvars",
]

ALLOWED_FILE_NAMES = [
    # The documented exceptions.
    "env.py",
    ".env.example",
    ".env.sample",
    ".env.template",
    ".ENV.EXAMPLE",
    ".env.local.example",
    ".env.production.sample",
    ".env-prod.template",
    "prod.env.example",
    # Near misses that must stay ordinary files.
    "env.example",
    "env.txt",
    "env.production.md",
    ".envelope",
    "environment.py",
    "secrets.py",
    "secrets.txt",
    "credentials.py",
    "credentials.txt",
    "keys.txt",
    "id_rsa.pub",
    "keystore.md",
    "tfstate.md",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".gitkeep",
    ".hgignore",
]

# Written out, not imported: a name dropped from the policy must fail here.
SENSITIVE_DIRECTORIES = [".aws", ".azure", ".docker", ".gnupg", ".kube", ".ssh"]
METADATA_DIRECTORIES = [".git", ".hg", ".svn"]


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
    write(path / "secret.txt", f"{MARKER}\n")
    return path


def json_size(model: object) -> int:
    assert hasattr(model, "model_dump_json")
    return len(model.model_dump_json().encode("utf-8"))


# The name rules, on their own


@pytest.mark.parametrize("name", SENSITIVE_FILE_NAMES)
def test_sensitive_name_is_recognized(name: str) -> None:
    assert is_sensitive_file_name(name) is True


@pytest.mark.parametrize("name", ALLOWED_FILE_NAMES)
def test_allowed_name_is_not_sensitive(name: str) -> None:
    assert is_sensitive_file_name(name) is False


@pytest.mark.parametrize("name", [".git.", ".git ", ".GIT..", ".ssh.", "node_modules."])
def test_trailing_dots_and_spaces_do_not_hide_a_directory_name(name: str) -> None:
    assert is_excluded_directory_name(name) is True
    assert refusal((name, "x"), Operation.CREATE) is not None


@pytest.mark.parametrize("name", [".env.", ".env ", "server.pem.", "server.pem "])
def test_trailing_dots_and_spaces_do_not_hide_a_sensitive_name(name: str) -> None:
    assert is_sensitive_file_name(name) is True


def test_refusal_table() -> None:
    assert refusal((), Operation.LIST) is None
    assert refusal((), Operation.READ) is None
    assert refusal(("src", "app.py"), Operation.LIST) is None
    assert refusal(("src", "app.py"), Operation.READ) is None
    assert refusal(("src", "app.py"), Operation.CREATE) is None
    assert refusal((".env",), Operation.LIST) is None
    assert refusal((".env",), Operation.READ) == "reading sensitive files is not allowed"
    assert refusal((".env",), Operation.CREATE) == "writing sensitive files is not allowed"
    assert refusal((".venv", "x.py"), Operation.LIST) is None
    assert refusal((".venv", "x.py"), Operation.READ) is None
    assert refusal((".venv", "x.py"), Operation.CREATE) == (
        "writing to excluded directories is not allowed"
    )
    for operation in Operation:
        assert refusal((".git",), operation) == "access to repository metadata is not allowed"
        assert refusal(("a", ".ssh", "b"), operation) == (
            "access to sensitive directories is not allowed"
        )


@pytest.mark.parametrize(
    "module_file", [workspace_module.__file__, filesystem_module.__file__], ids=["workspace", "fs"]
)
def test_only_the_policy_module_names_protected_paths(module_file: str) -> None:
    """A second copy of a rule could drift, so these modules must not contain any."""
    tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
    constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    protected = {".env", ".pem", ".key", "id_rsa", *SENSITIVE_DIRECTORIES, *METADATA_DIRECTORIES}
    assert not constants & protected


# Sensitive names, through every tool


@pytest.mark.parametrize("name", SENSITIVE_FILE_NAMES)
@pytest.mark.parametrize("directory", ["", "config/"])
def test_read_refuses_sensitive_name(
    workspace: Workspace, root: Path, name: str, directory: str
) -> None:
    write(root / f"{directory}{name}", MARKER)

    with pytest.raises(WorkspaceError, match="reading sensitive files is not allowed"):
        read_file(workspace, f"{directory}{name}")


@pytest.mark.parametrize("name", SENSITIVE_FILE_NAMES)
def test_read_refusal_does_not_reveal_existence(workspace: Workspace, name: str) -> None:
    with pytest.raises(WorkspaceError, match="reading sensitive files is not allowed"):
        read_file(workspace, f"missing/{name}")


@pytest.mark.parametrize("name", SENSITIVE_FILE_NAMES)
def test_search_skips_sensitive_name(workspace: Workspace, root: Path, name: str) -> None:
    write(root / "config" / name, MARKER)
    write(root / "plain.txt", MARKER)

    result = search_files(workspace, MARKER)

    assert [match.path for match in result.matches] == ["plain.txt"]
    assert result.skipped.sensitive_files == 1


@pytest.mark.parametrize("name", SENSITIVE_FILE_NAMES)
@pytest.mark.parametrize("directory", ["", "config/"])
def test_write_refuses_sensitive_name(
    workspace: Workspace, root: Path, name: str, directory: str
) -> None:
    (root / "config").mkdir()

    with pytest.raises(WorkspaceError, match="writing sensitive files is not allowed"):
        write_file(workspace, f"{directory}{name}", MARKER)

    assert not (root / f"{directory}{name}").exists()


@pytest.mark.parametrize("name", SENSITIVE_FILE_NAMES)
def test_list_shows_sensitive_name_but_not_content(
    workspace: Workspace, root: Path, name: str
) -> None:
    write(root / name, MARKER)

    result = list_files(workspace)

    assert [entry.path for entry in result.entries] == [name]
    assert MARKER not in result.model_dump_json()


# Allowed exceptions, through every tool


@pytest.mark.parametrize("name", ALLOWED_FILE_NAMES)
def test_read_allows_exception(workspace: Workspace, root: Path, name: str) -> None:
    write(root / "config" / name, MARKER)

    assert read_file(workspace, f"config/{name}").content == MARKER


@pytest.mark.parametrize("name", ALLOWED_FILE_NAMES)
def test_search_finds_exception(workspace: Workspace, root: Path, name: str) -> None:
    write(root / "config" / name, MARKER)

    result = search_files(workspace, MARKER)

    assert [match.path for match in result.matches] == [f"config/{name}"]
    assert result.skipped.sensitive_files == 0


@pytest.mark.parametrize("name", ALLOWED_FILE_NAMES)
def test_write_allows_exception(workspace: Workspace, root: Path, name: str) -> None:
    (root / "config").mkdir()

    assert write_file(workspace, f"config/{name}", MARKER).path == f"config/{name}"
    assert (root / "config" / name).read_bytes() == MARKER.encode()


@pytest.mark.parametrize("name", ALLOWED_FILE_NAMES)
def test_list_shows_exception(workspace: Workspace, root: Path, name: str) -> None:
    write(root / name, MARKER)

    assert [entry.path for entry in list_files(workspace).entries] == [name]


def test_dot_github_stays_readable(workspace: Workspace, root: Path) -> None:
    write(root / ".github" / "workflows" / "ci.yml", MARKER)

    assert read_file(workspace, ".github/workflows/ci.yml").content == MARKER
    assert [m.path for m in search_files(workspace, MARKER).matches] == [".github/workflows/ci.yml"]


def test_directory_with_a_sensitive_file_name_is_only_a_directory(
    workspace: Workspace, root: Path
) -> None:
    write(root / ".env" / "lib" / "module.py", MARKER)

    assert read_file(workspace, ".env/lib/module.py").content == MARKER
    assert [e.path for e in list_files(workspace, ".env").entries] == [".env/lib"]


# Repository metadata


METADATA_DENIED = "access to repository metadata is not allowed"


@pytest.mark.parametrize("name", METADATA_DIRECTORIES)
@pytest.mark.parametrize("prefix", ["", "sub/", "a/b/"])
def test_metadata_directory_is_denied_everywhere(
    workspace: Workspace, root: Path, name: str, prefix: str
) -> None:
    remote = f"[remote]\n\turl = https://user:{MARKER}@example.invalid/r\n"
    write(root / f"{prefix}{name}" / "config", remote)
    write(root / f"{prefix}{name}" / "hooks" / "pre-commit", MARKER)
    write(root / "plain.txt", "nothing")

    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        read_file(workspace, f"{prefix}{name}/config")
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        list_files(workspace, f"{prefix}{name}")
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        list_files(workspace, f"{prefix}{name}/hooks")
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        search_files(workspace, MARKER, f"{prefix}{name}")
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        write_file(workspace, f"{prefix}{name}/hooks/post-commit", MARKER)
    assert not (root / f"{prefix}{name}" / "hooks" / "post-commit").exists()

    result = search_files(workspace, MARKER)
    assert result.matches == []
    assert result.skipped.excluded_directories == 1
    walked = [entry.path for entry in list_files(workspace, recursive=True).entries]
    assert f"{prefix}{name}" in walked
    assert not [path for path in walked if path.startswith(f"{prefix}{name}/")]


@pytest.mark.parametrize("name", METADATA_DIRECTORIES)
def test_metadata_is_matched_case_insensitively(
    workspace: Workspace, root: Path, name: str
) -> None:
    write(root / name / "config", MARKER)
    upper = name.upper()

    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        read_file(workspace, f"{upper}/config")
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        list_files(workspace, upper)
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        search_files(workspace, MARKER, upper)


@pytest.mark.parametrize("name", METADATA_DIRECTORIES)
def test_metadata_file_form_is_denied(workspace: Workspace, root: Path, name: str) -> None:
    write(root / "sub" / name, f"gitdir: ../elsewhere {MARKER}\n")
    write(root / "plain.txt", "nothing")

    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        read_file(workspace, f"sub/{name}")
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        write_file(workspace, f"sub/{name}.d/{name}", "x")
    result = search_files(workspace, MARKER)
    assert result.matches == []
    assert result.skipped.sensitive_files == 1
    assert [(e.path, e.type) for e in list_files(workspace, "sub").entries] == [
        (f"sub/{name}", "file")
    ]


@pytest.mark.parametrize("name", METADATA_DIRECTORIES)
def test_metadata_name_cannot_be_created(workspace: Workspace, root: Path, name: str) -> None:
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        write_file(workspace, name, "gitdir: elsewhere\n")

    assert not (root / name).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows refuses these names outright")
@pytest.mark.parametrize("name", [".git.", ".git ", ".hg.", ".svn "])
def test_trailing_character_forms_of_metadata_are_denied_on_posix(
    workspace: Workspace, root: Path, name: str
) -> None:
    write(root / name / "config", MARKER)

    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        read_file(workspace, f"{name}/config")
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        list_files(workspace, name)


# Sensitive directories


SENSITIVE_DENIED = "access to sensitive directories is not allowed"


@pytest.mark.parametrize("name", SENSITIVE_DIRECTORIES)
@pytest.mark.parametrize("prefix", ["", "sub/"])
def test_sensitive_directory_is_denied_everywhere(
    workspace: Workspace, root: Path, name: str, prefix: str
) -> None:
    write(root / f"{prefix}{name}" / "config", MARKER)
    write(root / "plain.txt", "nothing")

    with pytest.raises(WorkspaceError, match=SENSITIVE_DENIED):
        read_file(workspace, f"{prefix}{name}/config")
    with pytest.raises(WorkspaceError, match=SENSITIVE_DENIED):
        list_files(workspace, f"{prefix}{name}")
    with pytest.raises(WorkspaceError, match=SENSITIVE_DENIED):
        search_files(workspace, MARKER, f"{prefix}{name}")
    with pytest.raises(WorkspaceError, match=SENSITIVE_DENIED):
        write_file(workspace, f"{prefix}{name}/new", MARKER)
    result = search_files(workspace, MARKER)
    assert result.matches == []
    assert result.skipped.excluded_directories == 1


@pytest.mark.parametrize("name", SENSITIVE_DIRECTORIES)
def test_file_named_like_a_sensitive_directory_is_denied(
    workspace: Workspace, root: Path, name: str
) -> None:
    write(root / name, MARKER)

    with pytest.raises(WorkspaceError, match=SENSITIVE_DENIED):
        read_file(workspace, name)
    assert search_files(workspace, MARKER).matches == []


# Generated directories keep their weaker rule


def test_generated_directory_is_readable_on_request_but_not_writable(
    workspace: Workspace, root: Path
) -> None:
    write(root / ".venv" / "lib" / "module.py", MARKER)

    assert read_file(workspace, ".venv/lib/module.py").content == MARKER
    assert [e.path for e in list_files(workspace, ".venv").entries] == [".venv/lib"]
    assert [m.path for m in search_files(workspace, MARKER, ".venv").matches] == [
        ".venv/lib/module.py"
    ]
    assert search_files(workspace, MARKER).matches == []
    with pytest.raises(WorkspaceError, match="writing to excluded directories"):
        write_file(workspace, ".venv/lib/new.py", "x")


def test_explicit_generated_directory_still_hides_metadata_inside_it(
    workspace: Workspace, root: Path
) -> None:
    write(root / ".venv" / ".git" / "config", MARKER)

    assert search_files(workspace, MARKER, ".venv").matches == []
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        read_file(workspace, ".venv/.git/config")


# Hard links


def test_read_refuses_hard_link_to_a_file_outside(
    workspace: Workspace, root: Path, outside: Path
) -> None:
    os.link(outside / "secret.txt", root / "notes.txt")

    with pytest.raises(HardLinkError, match=r"refusing to read hard-linked file: 'notes\.txt'"):
        read_file(workspace, "notes.txt")


def test_read_refuses_hard_link_to_a_sensitive_file(workspace: Workspace, root: Path) -> None:
    write(root / ".env", MARKER)
    os.link(root / ".env", root / "notes.txt")

    with pytest.raises(HardLinkError):
        read_file(workspace, "notes.txt")


def test_hard_link_refusal_covers_both_names(workspace: Workspace, root: Path) -> None:
    write(root / "one.txt", MARKER)
    assert read_file(workspace, "one.txt").content == MARKER  # Control: readable alone.
    os.link(root / "one.txt", root / "two.txt")

    for name in ("one.txt", "two.txt"):
        with pytest.raises(HardLinkError):
            read_file(workspace, name)


def test_file_is_readable_again_when_its_other_name_is_gone(
    workspace: Workspace, root: Path
) -> None:
    write(root / "one.txt", MARKER)
    os.link(root / "one.txt", root / "two.txt")
    (root / "two.txt").unlink()

    assert read_file(workspace, "one.txt").content == MARKER


def test_search_skips_and_counts_hard_links(
    workspace: Workspace, root: Path, outside: Path
) -> None:
    os.link(outside / "secret.txt", root / "notes.txt")
    write(root / "plain.txt", "nothing")

    result = search_files(workspace, MARKER)

    assert result.matches == []
    assert result.skipped.hard_links == 1
    assert result.files_scanned == 1


def test_list_shows_hard_link_as_a_file(workspace: Workspace, root: Path, outside: Path) -> None:
    os.link(outside / "secret.txt", root / "notes.txt")

    assert [(e.path, e.type) for e in list_files(workspace).entries] == [("notes.txt", "file")]


# Symlinks and junctions, across operations


@pytest.mark.parametrize("name", ["alias.txt", ".env"])
def test_file_symlink_is_refused_for_content(
    workspace: Workspace, root: Path, outside: Path, name: str
) -> None:
    link_file(root / name, outside / "secret.txt")

    with pytest.raises(WorkspaceError):
        read_file(workspace, name)
    assert search_files(workspace, MARKER).matches == []
    with pytest.raises(WorkspaceError, match=r"already exists|sensitive"):
        write_file(workspace, name, "x")
    assert (outside / "secret.txt").read_text(encoding="utf-8") == f"{MARKER}\n"


def test_symlink_to_inside_file_is_refused_too(workspace: Workspace, root: Path) -> None:
    write(root / "real.txt", MARKER)
    link_file(root / "alias.txt", root / "real.txt")

    with pytest.raises(WorkspaceError, match="refusing to follow symlink or junction"):
        read_file(workspace, "alias.txt")
    assert [m.path for m in search_files(workspace, MARKER).matches] == ["real.txt"]


def test_directory_link_is_refused_by_every_operation(
    workspace: Workspace, root: Path, outside: Path
) -> None:
    link_directory(root / "escape", outside)
    denied = "refusing to follow symlink or junction: 'escape'"

    with pytest.raises(WorkspaceError, match=denied):
        read_file(workspace, "escape/secret.txt")
    with pytest.raises(WorkspaceError, match=denied):
        list_files(workspace, "escape")
    with pytest.raises(WorkspaceError, match=denied):
        search_files(workspace, MARKER, "escape")
    with pytest.raises(WorkspaceError, match=denied):
        write_file(workspace, "escape/new.txt", "x")
    assert sorted(os.listdir(outside)) == ["secret.txt"]
    assert search_files(workspace, MARKER).matches == []


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_junction_is_refused_by_every_operation(
    workspace: Workspace, root: Path, outside: Path
) -> None:
    junction(root / "escape", outside)
    denied = "refusing to follow symlink or junction: 'escape'"

    assert [(e.path, e.type) for e in list_files(workspace).entries] == [("escape", "symlink")]
    with pytest.raises(WorkspaceError, match=denied):
        read_file(workspace, "escape/secret.txt")
    with pytest.raises(WorkspaceError, match=denied):
        list_files(workspace, "escape")
    with pytest.raises(WorkspaceError, match=denied):
        search_files(workspace, MARKER, "escape")
    with pytest.raises(WorkspaceError, match=denied):
        write_file(workspace, "escape/new.txt", "x")
    result = search_files(workspace, MARKER)
    assert result.matches == []
    assert result.skipped.symlinks == 1


def test_link_to_metadata_inside_the_workspace_is_refused(workspace: Workspace, root: Path) -> None:
    write(root / ".git" / "config", MARKER)
    link_directory(root / "alias", root / ".git")

    with pytest.raises(WorkspaceError, match="refusing to follow"):
        read_file(workspace, "alias/config")
    assert search_files(workspace, MARKER).matches == []


def test_symlink_tests_use_real_nt_symlinks(root: Path, outside: Path) -> None:
    """On Windows the symlink tests mean nothing unless these are true NT symlinks."""
    if sys.platform != "win32":
        pytest.skip("NT symlinks are a Windows feature")
    symlink_tag = 0xA000000C  # IO_REPARSE_TAG_SYMLINK
    link_file(root / "file-link", outside / "secret.txt")
    link_directory(root / "directory-link", outside)

    for name in ("file-link", "directory-link"):
        st = os.lstat(root / name)
        assert st.st_reparse_tag == symlink_tag, name
        assert stat.S_ISLNK(st.st_mode), name


# Windows 8.3 aliases


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.production",
        "deployment.pem",
        "credentials.json",
        "secrets.yaml",
        "terraform.tfstate",
        "production.tfvars",
        ".git-credentials",
        ".npmrc",
    ],
)
def test_short_name_alias_of_sensitive_file_is_refused(
    workspace: Workspace, root: Path, name: str
) -> None:
    target = write(root / name, MARKER)
    alias = short_name(target)

    with pytest.raises(WorkspaceError, match="reading sensitive files is not allowed") as excinfo:
        read_file(workspace, alias)

    assert MARKER not in str(excinfo.value)


@pytest.mark.parametrize("name", METADATA_DIRECTORIES)
def test_short_name_alias_of_metadata_directory_is_refused(
    workspace: Workspace, root: Path, name: str
) -> None:
    write(root / name / "config", MARKER)
    alias = short_name(root / name)

    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        read_file(workspace, f"{alias}/config")
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        list_files(workspace, alias)
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        search_files(workspace, MARKER, alias)
    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        write_file(workspace, f"{alias}/new.txt", "x")
    assert sorted(os.listdir(root / name)) == ["config"]


@pytest.mark.parametrize("name", METADATA_DIRECTORIES)
def test_short_name_alias_of_metadata_file_is_refused(
    workspace: Workspace, root: Path, name: str
) -> None:
    target = write(root / name, MARKER)
    alias = short_name(target)

    with pytest.raises(WorkspaceError, match=METADATA_DENIED):
        read_file(workspace, alias)


@pytest.mark.parametrize("name", SENSITIVE_DIRECTORIES)
def test_short_name_alias_of_sensitive_directory_is_refused(
    workspace: Workspace, root: Path, name: str
) -> None:
    write(root / name / "config", MARKER)
    alias = short_name(root / name)

    with pytest.raises(WorkspaceError, match=SENSITIVE_DENIED):
        read_file(workspace, f"{alias}/config")
    with pytest.raises(WorkspaceError, match=SENSITIVE_DENIED):
        list_files(workspace, alias)
    with pytest.raises(WorkspaceError, match=SENSITIVE_DENIED):
        write_file(workspace, f"{alias}/new.txt", "x")


def test_short_name_of_an_ordinary_file_still_works(workspace: Workspace, root: Path) -> None:
    """Control: the real-path check refuses by policy, not because an alias was used."""
    target = write(root / "ordinary-long-name.txt", MARKER)

    assert read_file(workspace, short_name(target)).content == MARKER


# Races: real changes on disk, between the check and the use


def swap_for_link(directory: Path, name: str, target: Path) -> None:
    (directory / name).rename(directory / f"{name}-real")
    link_directory(directory / name, target)


def before_open_child(
    monkeypatch: pytest.MonkeyPatch, name: str, action: Callable[[], None]
) -> None:
    """Run ``action`` once, after the traversal has decided to enter ``name``."""
    real = workspace_module._open_child
    pending = [True]

    def open_child(directory: workspace_module._Directory, child: str) -> int:
        if child == name and pending:
            pending.clear()
            action()
        return real(directory, child)

    monkeypatch.setattr(workspace_module, "_open_child", open_child)


@pytest.mark.parametrize(
    "operation",
    [
        lambda ws: read_file(ws, "sub/secret.txt"),
        lambda ws: list_files(ws, "sub"),
        lambda ws: search_files(ws, MARKER, "sub"),
        lambda ws: write_file(ws, "sub/new.txt", "x"),
    ],
    ids=["read", "list", "search", "write"],
)
def test_parent_swapped_during_traversal_is_refused(
    workspace: Workspace,
    root: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable[[Workspace], object],
) -> None:
    write(root / "sub" / "secret.txt", "inside")
    before_open_child(monkeypatch, "sub", lambda: swap_for_link(root, "sub", outside))

    with pytest.raises(WorkspaceError, match=r"directory changed|refusing to follow") as excinfo:
        operation(workspace)

    assert MARKER not in str(excinfo.value)
    assert sorted(os.listdir(outside)) == ["secret.txt"]


def test_deep_parent_swapped_during_traversal_is_refused(
    workspace: Workspace, root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "a" / "b" / "c.txt", "inside")
    write(outside / "c.txt", MARKER)
    before_open_child(monkeypatch, "b", lambda: swap_for_link(root / "a", "b", outside))

    with pytest.raises(WorkspaceError, match=r"directory changed|refusing to follow"):
        read_file(workspace, "a/b/c.txt")


def test_pinned_parent_keeps_a_read_inside_the_checked_directory(
    workspace: Workspace, root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once a parent is pinned, a swap cannot redirect the file open below it."""
    write(root / "sub" / "secret.txt", "inside")
    real_open = workspace_module._open_file
    blocked: list[str] = []

    def swap_then_open(directory: workspace_module._Directory, name: str) -> int:
        try:
            swap_for_link(root, "sub", outside)
        except PermissionError:
            blocked.append("sub")
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", swap_then_open)

    if sys.platform == "win32":
        # The swap cannot happen: the parent is held open.
        assert read_file(workspace, "sub/secret.txt").content == "inside"
        assert blocked == ["sub"]
    else:
        # The swap happens, but the open goes through the pinned descriptor, which still
        # refers to the original directory, so the link is never reached.
        assert read_file(workspace, "sub/secret.txt").content == "inside"
        assert blocked == []


def test_pinned_directory_listing_never_shows_the_swap_target(
    workspace: Workspace, root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "sub" / "inside.txt", "inside")
    real_scan = workspace_module._scan_entries

    def swap_then_scan(directory: workspace_module._Directory) -> list[os.DirEntry[str]]:
        with contextlib.suppress(PermissionError):
            swap_for_link(root, "sub", outside)
        return real_scan(directory)

    monkeypatch.setattr(workspace_module, "_scan_entries", swap_then_scan)

    assert [e.path for e in list_files(workspace, "sub").entries] == ["sub/inside.txt"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handles pin the path")
def test_every_pinned_component_blocks_rename_on_windows(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "a" / "b" / "c.txt", "inside")
    real_open = workspace_module._open_file
    refused: list[str] = []

    def rename_then_open(directory: workspace_module._Directory, name: str) -> int:
        for path in (root / "a" / "b", root / "a"):
            try:
                path.rename(path.with_name(f"{path.name}-moved"))
            except PermissionError:
                refused.append(path.name)
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", rename_then_open)

    assert read_file(workspace, "a/b/c.txt").content == "inside"
    assert refused == ["b", "a"]
    # Control: nothing else was holding them; the pin ended with the call.
    (root / "a" / "b").rename(root / "a" / "b-moved")


def test_file_swapped_for_symlink_before_open_is_refused(
    workspace: Workspace, root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "a.txt", "inside")
    link_file(root / "probe", outside / "secret.txt")
    (root / "probe").unlink()
    real_open = workspace_module._open_file

    def swap(directory: workspace_module._Directory, name: str) -> int:
        (root / "a.txt").unlink()
        (root / "a.txt").symlink_to(outside / "secret.txt")
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", swap)

    with pytest.raises(WorkspaceError, match=r"refusing to follow symlink or junction: 'a\.txt'"):
        read_file(workspace, "a.txt")


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_file_swapped_for_junction_before_open_is_refused(
    workspace: Workspace, root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "a.txt", "inside")
    real_open = workspace_module._open_file

    def swap(directory: workspace_module._Directory, name: str) -> int:
        (root / "a.txt").unlink()
        junction(root / "a.txt", outside)
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", swap)

    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "a.txt")

    assert MARKER not in str(excinfo.value)


def test_file_swapped_for_hard_link_before_open_is_refused(
    workspace: Workspace, root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "a.txt", "inside")
    real_open = workspace_module._open_file

    def swap(directory: workspace_module._Directory, name: str) -> int:
        (root / "a.txt").unlink()
        os.link(outside / "secret.txt", root / "a.txt")
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", swap)

    with pytest.raises(WorkspaceError, match="file changed while being opened"):
        read_file(workspace, "a.txt")


def test_search_entry_swapped_for_hard_link_after_the_scan_is_not_read(
    workspace: Workspace, root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "a.txt", "inside")
    real_open = workspace_module._open_file

    def swap(directory: workspace_module._Directory, name: str) -> int:
        (root / "a.txt").unlink()
        os.link(outside / "secret.txt", root / "a.txt")
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", swap)

    result = search_files(workspace, MARKER)

    assert result.matches == []
    assert result.skipped.hard_links + result.skipped.unreadable_files == 1


def make_fifo(path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("FIFOs are a POSIX feature")
    else:
        os.mkfifo(path)


@contextmanager
def fifo_watchdog(path: Path, seconds: float = 3.0) -> Iterator[threading.Event]:
    """Release an open that blocks on the FIFO at ``path``, and record that it had to.

    Opening a FIFO for reading waits for a writer. If the code under test blocks there,
    this acts as that writer, so the test finishes and fails instead of hanging.
    """
    finished = threading.Event()
    fired = threading.Event()

    def release() -> None:
        if finished.wait(seconds):
            return
        fired.set()
        while not finished.wait(0.1):
            with contextlib.suppress(OSError):
                os.close(os.open(path, os.O_WRONLY | getattr(os, "O_NONBLOCK", 0)))

    watchdog = threading.Thread(target=release, daemon=True)
    watchdog.start()
    try:
        yield fired
    finally:
        finished.set()
        watchdog.join()


def test_fifo_is_refused_without_blocking(workspace: Workspace, root: Path) -> None:
    make_fifo(root / "pipe")

    with fifo_watchdog(root / "pipe") as blocked:
        with pytest.raises(WorkspaceError, match=r"not a regular file: 'pipe'"):
            read_file(workspace, "pipe")
        assert [(e.path, e.type) for e in list_files(workspace).entries] == [("pipe", "other")]
        assert search_files(workspace, "x").files_scanned == 0

    assert not blocked.is_set()


def test_file_swapped_for_fifo_before_open_is_refused_without_blocking(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O_NONBLOCK is what keeps this open from waiting forever for a writer."""
    write(root / "a.txt", "inside")
    if sys.platform == "win32":
        pytest.skip("FIFOs are a POSIX feature")
    real_open = workspace_module._open_file

    def swap(directory: workspace_module._Directory, name: str) -> int:
        (root / "a.txt").unlink()
        make_fifo(root / "a.txt")
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", swap)

    with (
        fifo_watchdog(root / "a.txt") as blocked,
        pytest.raises(WorkspaceError, match="file changed while being opened"),
    ):
        read_file(workspace, "a.txt")

    assert not blocked.is_set()


def test_read_stops_at_the_limit_when_a_file_grows_after_the_size_check(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write(root / "a.txt", "x" * 10)
    real_read = workspace_module._read_up_to

    def grow_then_read(fd: int, limit: int) -> bytes:
        target.write_bytes(b"x" * 100)
        return real_read(fd, limit)

    monkeypatch.setattr(workspace_module, "_read_up_to", grow_then_read)

    with pytest.raises(WorkspaceError, match="exceeds the limit of 10 bytes"):
        read_file(workspace, "a.txt", limits=FilesystemLimits(max_read_bytes=10))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle inspection")
def test_redirect_seen_through_an_open_handle_is_refused(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated: reports a symlink's attributes, for machines that cannot create one."""
    write(root / "a.txt", "inside")
    symlink_tag = 0xA000000C
    real = vars(workspace_module)["_attributes_of"]

    def as_symlink(handle: int) -> object:
        info = real(handle)
        info.attributes |= workspace_module._FILE_ATTRIBUTE_REPARSE_POINT
        info.reparse_tag = symlink_tag
        return info

    monkeypatch.setattr(workspace_module, "_attributes_of", as_symlink)

    with pytest.raises(WorkspaceError, match="refusing to follow symlink or junction"):
        read_file(workspace, "a.txt")


def test_other_reparse_points_are_ordinary_files(workspace: Workspace, root: Path) -> None:
    """A reparse point that is not a name surrogate, such as a cloud file, is not a link."""
    assert workspace_module._is_redirect(0x400, 0xA0000003) is True
    assert workspace_module._is_redirect(0x400, 0xA000000C) is True
    assert workspace_module._is_redirect(0x400, 0x9000001A) is False
    assert workspace_module._is_redirect(0x20, 0) is False


def open_handles() -> int:
    """Return how many descriptors or handles this process has open."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        count = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        assert kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    if sys.platform == "linux":
        return len(os.listdir("/proc/self/fd"))
    pytest.skip("no handle count on this platform")


def test_no_operation_leaks_a_handle(workspace: Workspace, root: Path, outside: Path) -> None:
    """A leaked Windows handle would pin a directory until the server exits."""
    write(root / "a" / "b" / "ok.txt", "needle")
    write(root / "a" / ".env", "needle")
    write(root / "a" / "big.txt", "x" * 100)
    os.link(outside / "secret.txt", root / "a" / "linked.txt")
    limits = FilesystemLimits(max_read_bytes=50)

    def exercise() -> None:
        for path in ("a/b/ok.txt", "a/.env", "a/big.txt", "a/linked.txt", "a/b", "a/missing"):
            with contextlib.suppress(WorkspaceError):
                read_file(workspace, path, limits=limits)
        list_files(workspace, recursive=True)
        search_files(workspace, "needle")
        for path in ("a/new.txt", "a/b/ok.txt", "missing/new.txt", "a/b/ok.txt/new.txt"):
            with contextlib.suppress(WorkspaceError):
                write_file(workspace, path, "x")
        with contextlib.suppress(FileNotFoundError):
            (root / "a" / "new.txt").unlink()

    exercise()
    before = open_handles()
    for _ in range(25):
        exercise()

    assert open_handles() == before
    # On Windows a leaked pin would make this fail too.
    (root / "a").rename(root / "a-moved")


# Output limits


def test_read_at_the_response_limit(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "line\n" * 20)
    size = json_size(read_file(workspace, "a.txt"))

    fits = read_file(workspace, "a.txt", limits=FilesystemLimits(max_response_bytes=size))

    assert json_size(fits) == size
    with pytest.raises(WorkspaceError, match=f"is {size} bytes as JSON; the limit is {size - 1}"):
        read_file(workspace, "a.txt", limits=FilesystemLimits(max_response_bytes=size - 1))


def test_default_response_limit_refuses_a_file_whose_json_expands(
    workspace: Workspace, root: Path
) -> None:
    """A file within the read limit can still be far larger once encoded as JSON."""
    write(root / "newlines.txt", b"\n" * (1024 * 1024))

    with pytest.raises(WorkspaceError, match=r"response is too large: 'newlines\.txt'"):
        read_file(workspace, "newlines.txt")


def test_default_read_limit_boundary(workspace: Workspace, root: Path) -> None:
    write(root / "fits.txt", b"x" * (1024 * 1024))
    write(root / "big.txt", b"x" * (1024 * 1024 + 1))

    assert read_file(workspace, "fits.txt").size_bytes == 1024 * 1024
    with pytest.raises(WorkspaceError, match="the limit is 1048576 bytes"):
        read_file(workspace, "big.txt")


def smallest_complete_limit(call: Callable[[FilesystemLimits], object], start: int) -> int:
    """Return the smallest response limit at which ``call`` is not truncated."""
    for limit in range(start, 0, -1):
        result = call(FilesystemLimits(max_response_bytes=limit - 1))
        assert hasattr(result, "truncated")
        if result.truncated:
            return limit
    pytest.fail("no response limit truncated the result")


def test_list_at_and_beyond_the_response_limit(workspace: Workspace, root: Path) -> None:
    for index in range(6):
        write(root / f"file{index}.txt", "")

    def call(limits: FilesystemLimits) -> ListFilesResult:
        return list_files(workspace, limits=limits)

    boundary = smallest_complete_limit(call, json_size(call(FilesystemLimits())) + 200)

    # The boundary is exact: the largest form of the empty result, plus the items and commas.
    wide = call(FilesystemLimits())
    largest_empty = wide.model_copy(
        update={
            "entries": [],
            "truncated": True,
            "truncation_reason": "max_response_bytes",
            "skipped_directories": 10**18,
        }
    )
    items = ",".join(entry.model_dump_json() for entry in wide.entries)
    assert boundary == json_size(largest_empty) + len(items.encode("utf-8"))

    at_limit = call(FilesystemLimits(max_response_bytes=boundary))
    assert len(at_limit.entries) == 6
    assert at_limit.truncated is False
    assert json_size(at_limit) <= boundary
    beyond = call(FilesystemLimits(max_response_bytes=boundary - 1))
    assert len(beyond.entries) == 5
    assert beyond.truncated is True
    assert beyond.truncation_reason == "max_response_bytes"
    assert json_size(beyond) <= boundary - 1
    for limit in range(boundary - 60, boundary + 2):
        assert json_size(call(FilesystemLimits(max_response_bytes=limit))) <= limit


def test_search_at_and_beyond_the_response_limit(workspace: Workspace, root: Path) -> None:
    for index in range(6):
        write(root / f"file{index}.txt", "needle here\n")

    def call(limits: FilesystemLimits) -> SearchFilesResult:
        return search_files(workspace, "needle", limits=limits)

    boundary = smallest_complete_limit(call, json_size(call(FilesystemLimits())) + 400)

    # The boundary is exact: the largest form of the empty result, plus the items and commas.
    wide = call(FilesystemLimits())
    largest_empty = wide.model_copy(
        update={
            "matches": [],
            "files_scanned": 10**18,
            "truncated": True,
            "truncation_reason": "max_response_bytes",
            "skipped": wide.skipped.model_copy(
                update=dict.fromkeys(type(wide.skipped).model_fields, 10**18)
            ),
        }
    )
    items = ",".join(match.model_dump_json() for match in wide.matches)
    assert boundary == json_size(largest_empty) + len(items.encode("utf-8"))

    at_limit = call(FilesystemLimits(max_response_bytes=boundary))
    assert len(at_limit.matches) == 6
    assert at_limit.truncated is False
    assert json_size(at_limit) <= boundary
    beyond = call(FilesystemLimits(max_response_bytes=boundary - 1))
    assert len(beyond.matches) == 5
    assert beyond.truncation_reason == "max_response_bytes"
    assert json_size(beyond) <= boundary - 1
    for limit in range(boundary - 60, boundary + 2):
        assert json_size(call(FilesystemLimits(max_response_bytes=limit))) <= limit


def test_response_limit_counts_bytes_not_characters(workspace: Workspace, root: Path) -> None:
    for index in range(4):
        write(root / "aaaa" / f"xx{index}.txt", "")
        write(root / "bbbb" / f"世界{index}.txt", "")

    def boundary(path: str) -> int:
        return smallest_complete_limit(
            lambda limits: list_files(workspace, path, limits=limits), 900
        )

    # Each of the four names has two characters that take three bytes instead of one.
    assert boundary("bbbb") - boundary("aaaa") == 16


def test_list_reports_why_it_stopped(workspace: Workspace, root: Path) -> None:
    for index in range(3):
        write(root / f"file{index}.txt", "")

    assert list_files(workspace, max_entries=2).truncation_reason == "max_entries"
    assert list_files(workspace, max_entries=3).truncation_reason is None


@pytest.mark.parametrize(
    "call",
    [
        lambda ws, path: read_file(ws, path),
        lambda ws, path: list_files(ws, path),
        lambda ws, path: search_files(ws, "x", path),
        lambda ws, path: write_file(ws, path, "x"),
    ],
    ids=["read", "list", "search", "write"],
)
def test_path_length_limit_bounds_what_an_error_can_echo(
    workspace: Workspace, call: Callable[[Workspace, str], object]
) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        call(workspace, "x" * (MAX_PATH_CHARS + 1))

    assert str(excinfo.value) == f"path is too long: the limit is {MAX_PATH_CHARS} characters"


def test_path_at_the_length_limit_is_only_not_found(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        read_file(workspace, "a/" * (MAX_PATH_CHARS // 2))

    assert "path is too long" not in str(excinfo.value)


# Untrusted output


@pytest.mark.parametrize(
    ("raw", "escaped", "count"),
    [
        ("plain text\n", "plain text\n", 0),
        ("tab\tcr\r\nlf\n", "tab\tcr\r\nlf\n", 0),
        ("\x1b[31mred\x1b[0m", "\\x1b[31mred\\x1b[0m", 2),
        ("bell\x07", "bell\\x07", 1),
        ("back\x08space", "back\\x08space", 1),
        ("form\x0cfeed\x0bvt", "form\\x0cfeed\\x0bvt", 2),
        ("del\x7f c1\x9b", "del\\x7f c1\\x9b", 2),
        ("rlo\u202egnp.exe", "rlo\\u202egnp.exe", 1),
        ("iso\u2066x\u2069", "iso\\u2066x\\u2069", 2),
        ("line\u2028sep\u2029", "line\\u2028sep\\u2029", 2),
        ("tag\U000e0041\U000e007f", "tag\\U000e0041\\U000e007f", 2),
        ("lone\udc80", "lone\\udc80", 1),
        ("keep \ufeff and \u200d", "keep \ufeff and \u200d", 0),
        ("世界 🎉 é", "世界 🎉 é", 0),
    ],
)
def test_escape_text(raw: str, escaped: str, count: int) -> None:
    assert escape_text(raw) == (escaped, count)


def test_escape_line_also_escapes_line_endings() -> None:
    assert escape_line("a\nb\rc\td") == "a\\x0ab\\x0dc\td"


def test_read_escapes_and_counts(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "red \x1b[31m\nbidi \u202e\ttag \U000e0041\r\n")

    result = read_file(workspace, "a.txt")

    assert result.content == "red \\x1b[31m\nbidi \\u202e\ttag \\U000e0041\r\n"
    assert result.escaped_characters == 3
    assert result.trust == "untrusted"


def test_literal_backslash_text_is_distinguishable_from_an_escape(
    workspace: Workspace, root: Path
) -> None:
    write(root / "a.py", 'ESC = "\\x1b"\n')

    result = read_file(workspace, "a.py")

    assert result.content == 'ESC = "\\x1b"\n'
    assert result.escaped_characters == 0


def test_search_snippet_is_escaped_and_stays_on_one_line(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "needle \x1b[2J over\rwrite \u202e end\n")

    (match,) = search_files(workspace, "needle").matches

    assert match.snippet == "needle \\x1b[2J over\\x0dwrite \\u202e end"
    assert match.line == 1
    assert match.column == 1


def test_search_of_a_long_line_escapes_the_shortened_snippet(
    workspace: Workspace, root: Path
) -> None:
    write(root / "a.txt", "\x1b" * 300 + "needle" + "\x1b" * 300)

    (match,) = search_files(workspace, "needle").matches

    assert "\x1b" not in match.snippet
    assert "needle" in match.snippet
    assert match.column == 301


def test_results_are_labelled_untrusted(workspace: Workspace, root: Path) -> None:
    write(root / "a.txt", "needle")

    for result in (
        list_files(workspace),
        read_file(workspace, "a.txt"),
        search_files(workspace, "needle"),
    ):
        assert json.loads(result.model_dump_json())["trust"] == "untrusted"


@pytest.mark.parametrize("name", ["rlo\u202egnp.txt", "tag\U000e0041.txt"])
def test_listed_and_matched_names_are_escaped(workspace: Workspace, root: Path, name: str) -> None:
    write(root / name, "needle")
    shown = escape_line(name)

    assert shown != name
    assert [e.path for e in list_files(workspace).entries] == [shown]
    assert [m.path for m in search_files(workspace, "needle").matches] == [shown]
    assert read_file(workspace, name).path == shown


@pytest.mark.skipif(sys.platform == "win32", reason="Windows forbids control characters in names")
def test_name_with_a_newline_cannot_forge_a_second_entry(workspace: Workspace, root: Path) -> None:
    write(root / "a\nfake-entry.txt", "needle")

    assert [e.path for e in list_files(workspace).entries] == ["a\\x0afake-entry.txt"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX names are raw bytes")
def test_undecodable_name_can_be_listed_and_serialized(workspace: Workspace, root: Path) -> None:
    with open(os.path.join(os.fsencode(root), b"bad\xff.txt"), "wb") as handle:
        handle.write(b"needle")

    listing = list_files(workspace)
    found = search_files(workspace, "needle")

    assert [e.path for e in listing.entries] == ["bad\\udcff.txt"]
    assert [m.path for m in found.matches] == ["bad\\udcff.txt"]
    json.loads(listing.model_dump_json())
    json.loads(found.model_dump_json())


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS names can hold lone surrogates")
def test_lone_surrogate_name_can_be_listed_and_serialized(workspace: Workspace, root: Path) -> None:
    write(root / "bad\ud800.txt", "needle")

    listing = list_files(workspace)

    assert [e.path for e in listing.entries] == ["bad\\ud800.txt"]
    json.loads(listing.model_dump_json())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows encodes any string as a path")
def test_path_that_the_filesystem_cannot_encode_is_refused(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match=r"path is not valid text for this filesystem"):
        read_file(workspace, "bad\ud800.txt")


def test_search_echoes_an_unsafe_pattern_escaped(workspace: Workspace) -> None:
    result = search_files(workspace, "a\x1bb\ud800")

    assert result.pattern == "a\\x1bb\\ud800"
    json.loads(result.model_dump_json())

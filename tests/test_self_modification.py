"""The server never changes its own code, its Python environment, or its configuration.

The workspace in these tests is a checkout of a repository that contains the server's code
(``src/universal_ai_tools``), a directory on the import path (``src``), and a Python
environment (``runtime-env``), as when someone points Universal AI Tools at its own
repository with an editable install. Every tool that can change files tries to change a
protected file, through every path spelling the platform allows, and must leave every byte
as it was. Ordinary project files in the same workspace stay writable.

``TrustedPaths`` are given explicitly for the checkout, so that a broken safeguard can only
damage the test's own copy; the last tests check that the running server finds the real
locations on its own.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

import universal_ai_tools
from service_support import Dialog, dialog_server_kwargs, service, write_config
from support import junction, link_file, short_name, write
from universal_ai_tools import file_actions, filesystem
from universal_ai_tools.audit import read_records
from universal_ai_tools.clients import server_entry
from universal_ai_tools.filesystem import DEFAULT_LIMITS
from universal_ai_tools.server import create_server
from universal_ai_tools.settings import load_config_file
from universal_ai_tools.trusted import (
    IMPORT_PATH,
    PYTHON_ENVIRONMENT,
    SERVER_CODE,
    TrustedPaths,
    import_path_contains,
    normalize,
)
from universal_ai_tools.workspace import (
    HardLinkError,
    LinkRefusedError,
    ProtectedPathError,
    Workspace,
    WorkspaceError,
)

GATE_SOURCE = "def approve(decision):\n    return decision == 'approve'\n"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    package = root / "src" / "universal_ai_tools"
    write(package / "__init__.py", "")
    write(package / "gate.py", GATE_SOURCE)
    write(package / "approval.py", "APPROVE = 'approve'\n")
    write(package / "policy.py", "REFUSED = ('.git',)\n")
    write(package / "workspace.py", "ROOT = None\n")
    write(root / "src" / "helper.py", "VALUE = 1\n")
    write(root / "runtime-env" / "pyvenv.cfg", "home = C:\\Python313\n")
    write(root / "runtime-env" / "Lib" / "site-packages" / "editable.pth", "../../src\n")
    write(root / "docs" / "notes.txt", "notes\n")
    write(root / "tests" / "test_x.py", "def test(): pass\n")
    return root


def trusted_for(repo: Path) -> TrustedPaths:
    return TrustedPaths(
        [
            (repo / "src" / "universal_ai_tools", SERVER_CODE),
            (repo / "src", IMPORT_PATH, True),
            (repo / "runtime-env", PYTHON_ENVIRONMENT, True),
            (repo / "runtime-env" / "Lib" / "site-packages", IMPORT_PATH, True),
        ]
    )


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Every file and directory under ``root``, with file contents."""
    found: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        found[key] = path.read_bytes() if path.is_file() else None
    return found


# Every changing tool, against every kind of protected location.

ATTEMPTS: list[tuple[str, dict[str, object]]] = [
    ("write_file", {"path": "src/universal_ai_tools/evil.py", "content": "x = 1\n"}),
    ("write_file", {"path": "src/sitecustomize.py", "content": "import os\n"}),
    ("write_file", {"path": "src/json.py", "content": "loads = None\n"}),
    ("write_file", {"path": "runtime-env/Lib/site-packages/evil.pth", "content": "import x\n"}),
    ("write_file", {"path": "runtime-env/new.cfg", "content": "x\n"}),
    (
        "edit_file",
        {"path": "src/universal_ai_tools/gate.py", "old_text": "== 'approve'", "new_text": "or 1"},
    ),
    (
        "edit_file",
        {
            "path": "src/universal_ai_tools/approval.py",
            "old_text": "'",
            "new_text": '"',
            "expected_replacements": 2,
        },
    ),
    ("edit_file", {"path": "src/helper.py", "old_text": "1", "new_text": "2"}),
    ("edit_file", {"path": "runtime-env/pyvenv.cfg", "old_text": "home", "new_text": "evil"}),
    (
        "edit_file",
        {"path": "runtime-env/Lib/site-packages/editable.pth", "old_text": "src", "new_text": "x"},
    ),
    ("create_directory", {"path": "src/universal_ai_tools/plugins"}),
    ("create_directory", {"path": "src/simplejson"}),
    ("create_directory", {"path": "runtime-env/Scripts"}),
    ("move_path", {"source": "src/universal_ai_tools/gate.py", "destination": "docs/gate.py"}),
    ("move_path", {"source": "docs/notes.txt", "destination": "src/universal_ai_tools/notes.py"}),
    ("move_path", {"source": "docs/notes.txt", "destination": "src/notes.py"}),
    ("move_path", {"source": "src/universal_ai_tools", "destination": "docs/moved-package"}),
    ("move_path", {"source": "src", "destination": "old-src"}),
    ("move_path", {"source": "runtime-env", "destination": "docs/env"}),
    ("move_path", {"source": "runtime-env/pyvenv.cfg", "destination": "docs/pyvenv.cfg"}),
]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments"), ATTEMPTS, ids=[f"{t}:{i}" for i, (t, _) in enumerate(ATTEMPTS)]
)
async def test_no_tool_changes_trusted_code_even_when_approved(
    repo: Path, tmp_path: Path, tool: str, arguments: dict[str, object]
) -> None:
    config = service(tmp_path / "state", repo, {"filesystem": {"write": True}})
    before = snapshot(repo)
    dialog = Dialog("yes")  # the human approves: the server must still refuse
    server = create_server(config, trusted=trusted_for(repo), **dialog_server_kwargs(dialog))

    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)

    message = " ".join(c.text for c in result.content if isinstance(c, TextContent))
    assert result.is_error is True, message
    assert "never modifies" in message
    assert snapshot(repo) == before
    finished = [
        r for r in read_records(tmp_path / "state" / "audit.jsonl") if r["event"] == "finished"
    ]
    assert finished[-1]["error_class"] == "protected_path"


@pytest.mark.anyio
async def test_ordinary_project_files_stay_writable(repo: Path, tmp_path: Path) -> None:
    config = service(tmp_path / "state", repo, {"filesystem": {"write": True}})
    server = create_server(config, trusted=trusted_for(repo), **dialog_server_kwargs(Dialog("yes")))

    async with Client(server) as client:
        results = [
            await client.call_tool("write_file", {"path": "docs/new.txt", "content": "new\n"}),
            await client.call_tool(
                "edit_file", {"path": "docs/notes.txt", "old_text": "notes", "new_text": "edited"}
            ),
            await client.call_tool("create_directory", {"path": "docs/sub"}),
            await client.call_tool(
                "move_path", {"source": "docs/notes.txt", "destination": "tests/notes.txt"}
            ),
            await client.call_tool("read_file", {"path": "src/universal_ai_tools/gate.py"}),
        ]

    assert [r.is_error for r in results] == [False] * 5
    assert (repo / "tests" / "notes.txt").read_bytes() == b"edited\n"
    assert (repo / "docs" / "new.txt").read_bytes() == b"new\n"


# Other spellings of the same file.


def workspace_for(repo: Path) -> Workspace:
    return Workspace(repo, trusted=trusted_for(repo))


def edit(workspace: Workspace, path: str, old: str = "approve", new: str = "allow") -> None:
    arguments = file_actions.EditFileInput(
        path=path, old_text=old, new_text=new, expected_replacements=2
    )
    file_actions.edit_file(workspace, arguments, DEFAULT_LIMITS)


@pytest.mark.parametrize(
    "spelling",
    [
        "src/universal_ai_tools/gate.py",
        "src\\universal_ai_tools\\gate.py",
        "./src/./universal_ai_tools//gate.py",
        "src/universal_ai_tools\\./gate.py",
    ],
)
def test_every_spelling_of_a_protected_file_is_refused(repo: Path, spelling: str) -> None:
    before = snapshot(repo)

    with pytest.raises(ProtectedPathError):
        edit(workspace_for(repo), spelling)

    assert snapshot(repo) == before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ignores case")
def test_another_case_of_a_protected_file_is_refused(repo: Path) -> None:
    before = snapshot(repo)

    with pytest.raises(ProtectedPathError):
        edit(workspace_for(repo), "SRC/Universal_AI_Tools/GATE.PY")

    assert snapshot(repo) == before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ignores trailing dots and spaces")
@pytest.mark.parametrize(
    "spelling",
    [
        "src/universal_ai_tools/gate.py.",
        "src/universal_ai_tools/gate.py ",
        "src./universal_ai_tools/gate.py",
        "src/universal_ai_tools /gate.py",
    ],
)
def test_names_windows_would_shorten_are_refused(repo: Path, spelling: str) -> None:
    before = snapshot(repo)

    with pytest.raises(WorkspaceError):
        edit(workspace_for(repo), spelling)
    with pytest.raises(WorkspaceError):
        filesystem.write_file(
            workspace_for(repo), spelling.replace("gate.py", "new.py"), "x\n", limits=DEFAULT_LIMITS
        )

    assert snapshot(repo) == before


def test_an_8dot3_short_name_of_a_protected_directory_is_refused(repo: Path) -> None:
    short = short_name(repo / "src" / "universal_ai_tools")
    before = snapshot(repo)
    workspace = workspace_for(repo)

    with pytest.raises(ProtectedPathError):
        edit(workspace, f"src/{short}/gate.py")
    with pytest.raises(ProtectedPathError):
        filesystem.write_file(workspace, f"src/{short}/evil.py", "x\n", limits=DEFAULT_LIMITS)
    assert snapshot(repo) == before


def test_a_junction_into_protected_code_is_refused(repo: Path) -> None:
    junction(repo / "docs" / "code", repo / "src" / "universal_ai_tools")
    workspace = workspace_for(repo)

    with pytest.raises(LinkRefusedError):
        filesystem.write_file(workspace, "docs/code/evil.py", "x\n", limits=DEFAULT_LIMITS)
    with pytest.raises(LinkRefusedError):
        edit(workspace, "docs/code/gate.py")
    assert (repo / "src" / "universal_ai_tools" / "gate.py").read_text() == GATE_SOURCE
    assert not (repo / "src" / "universal_ai_tools" / "evil.py").exists()


def test_a_symlink_to_protected_code_is_refused(repo: Path) -> None:
    link_file(repo / "docs" / "gate.py", repo / "src" / "universal_ai_tools" / "gate.py")

    with pytest.raises(LinkRefusedError):
        edit(workspace_for(repo), "docs/gate.py")
    assert (repo / "src" / "universal_ai_tools" / "gate.py").read_text() == GATE_SOURCE


def test_a_hard_link_to_protected_code_is_refused(repo: Path) -> None:
    target = repo / "src" / "universal_ai_tools" / "gate.py"
    (repo / "docs" / "gate.py").hardlink_to(target)

    with pytest.raises(HardLinkError):
        edit(workspace_for(repo), "docs/gate.py")
    assert target.read_text() == GATE_SOURCE


def test_a_protected_location_given_through_a_junction_is_still_found(
    repo: Path, tmp_path: Path
) -> None:
    alias = tmp_path / "alias"
    junction(alias, repo / "src" / "universal_ai_tools")
    workspace = Workspace(repo, trusted=TrustedPaths([(alias, SERVER_CODE)]))

    with pytest.raises(ProtectedPathError):
        edit(workspace, "src/universal_ai_tools/gate.py")
    with pytest.raises(ProtectedPathError):
        workspace.move("src/universal_ai_tools", "docs/moved")


# Which locations protect which paths.


def test_an_import_path_above_the_workspace_leaves_it_writable(tmp_path: Path) -> None:
    project = (tmp_path / "home" / "project").resolve()
    write(project / "notes.txt", "x\n")
    trusted = TrustedPaths([(tmp_path / "home", IMPORT_PATH, True)])

    filesystem.write_file(
        Workspace(project, trusted=trusted), "new.txt", "ok\n", limits=DEFAULT_LIMITS
    )

    assert (project / "new.txt").read_bytes() == b"ok\n"


def test_the_workspace_itself_on_the_import_path_is_all_protected(repo: Path) -> None:
    """A server started in the workspace without -I imports from it: nothing may change."""
    workspace = Workspace(repo, trusted=TrustedPaths([(repo, IMPORT_PATH, True)]))

    with pytest.raises(ProtectedPathError):
        filesystem.write_file(workspace, "docs/new.txt", "x\n", limits=DEFAULT_LIMITS)
    with pytest.raises(ProtectedPathError):
        workspace.make_directory("json")


def test_a_workspace_inside_the_server_code_is_all_protected(repo: Path) -> None:
    package = repo / "src" / "universal_ai_tools"
    workspace = Workspace(package, trusted=TrustedPaths([(package, SERVER_CODE)]))

    with pytest.raises(ProtectedPathError):
        filesystem.write_file(workspace, "new.py", "x\n", limits=DEFAULT_LIMITS)


def test_moving_a_directory_that_holds_protected_code_is_refused(repo: Path) -> None:
    workspace = Workspace(
        repo, trusted=TrustedPaths([(repo / "src" / "universal_ai_tools", SERVER_CODE)])
    )

    with pytest.raises(ProtectedPathError):
        workspace.move("src", "elsewhere")
    workspace.move("docs", "documents")  # holds nothing protected

    assert (repo / "src" / "universal_ai_tools" / "gate.py").exists()


# What the running server protects without being told.


def test_the_running_server_protects_its_own_code_and_environment() -> None:
    trusted = TrustedPaths.for_runtime()
    protected = {entry.path for entry in trusted}

    assert normalize(Path(universal_ai_tools.__file__).resolve().parent) in protected
    assert normalize(Path(sys.prefix).resolve()) in protected
    assert normalize(Path(sys.executable).resolve().parent) in protected
    import json

    assert normalize(Path(json.__file__).resolve().parent) in protected
    for entry in sys.path:
        if entry and Path(entry).is_dir():
            assert normalize(Path(entry).resolve()) in protected, entry


def test_a_new_import_path_and_module_are_protected(tmp_path: Path) -> None:
    site = (tmp_path / "site").resolve()
    name = f"uat_probe_{uuid.uuid4().hex}"
    write(site / f"{name}.py", "VALUE = 1\n")
    sys.path.append(str(site))
    try:
        __import__(name)
        trusted = TrustedPaths.for_runtime()
    finally:
        sys.path.remove(str(site))
        sys.modules.pop(name, None)
    workspace = Workspace(tmp_path.resolve(), trusted=trusted)

    with pytest.raises(ProtectedPathError):
        filesystem.write_file(workspace, "site/sitecustomize.py", "x\n", limits=DEFAULT_LIMITS)
    with pytest.raises(ProtectedPathError):
        edit(workspace, f"site/{name}.py", "1", "2")
    filesystem.write_file(workspace, "other.txt", "fine\n", limits=DEFAULT_LIMITS)


def test_the_real_checkout_cannot_change_the_running_server() -> None:
    """The workspace is the checkout the server was imported from, as in development."""
    package = Path(universal_ai_tools.__file__).resolve().parent
    checkout = package.parent.parent
    workspace = Workspace(checkout)
    probe = f"uat-self-modification-probe-{uuid.uuid4().hex}"
    relative = package.relative_to(checkout).as_posix()
    try:
        with pytest.raises(ProtectedPathError):
            filesystem.write_file(
                workspace, f"{relative}/{probe}.py", "x = 1\n", limits=DEFAULT_LIMITS
            )
        with pytest.raises(ProtectedPathError):
            workspace.make_directory(f"{relative}/{probe}")
    finally:
        # Only reached if the safeguard failed: remove what the probe made.
        (package / f"{probe}.py").unlink(missing_ok=True)
        if (package / probe).is_dir():
            (package / probe).rmdir()


def test_file_mode_protects_the_configuration_and_audit_log(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = load_config_file(write_config(tmp_path / "state", workspace))
    protected = {entry.path for entry in TrustedPaths.for_service(config)}

    assert config.config_path is not None
    assert config.audit_path is not None
    assert normalize(config.config_path.resolve()) in protected
    assert normalize(config.config_path.parent.resolve()) in protected
    assert normalize(config.audit_path.resolve()) in protected


# Starting the server so that the working directory cannot replace its code.


def test_clients_start_the_server_isolated(tmp_path: Path) -> None:
    entry: dict[str, Any] = server_entry(tmp_path / "config.toml", python="python")

    assert entry["args"][0] == "-I"


def test_isolated_python_does_not_import_from_the_working_directory(tmp_path: Path) -> None:
    write(tmp_path / "json.py", "PLANTED = True\n")
    probe = "import json; print(hasattr(json, 'PLANTED'))"

    plain = subprocess.run(
        [sys.executable, "-c", probe], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    isolated = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert plain.stdout.strip() == "True"  # what -I prevents
    assert isolated.stdout.strip() == "False"


def test_the_import_path_check_finds_a_directory_python_imports_from(tmp_path: Path) -> None:
    assert not import_path_contains(tmp_path)
    sys.path.append(str(tmp_path))
    try:
        assert import_path_contains(tmp_path)
    finally:
        sys.path.remove(str(tmp_path))


def test_a_module_loaded_from_outside_the_import_path_is_protected(tmp_path: Path) -> None:
    import importlib.util

    plugin = (tmp_path / "plugin").resolve()
    name = f"uat_loaded_{uuid.uuid4().hex}"
    write(plugin / name / "__init__.py", "VALUE = 1\n")
    spec = importlib.util.spec_from_file_location(name, plugin / name / "__init__.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        trusted = TrustedPaths.for_runtime()
    finally:
        sys.modules.pop(name, None)
    workspace = Workspace(tmp_path.resolve(), trusted=trusted)

    with pytest.raises(ProtectedPathError):
        filesystem.write_file(workspace, f"plugin/{name}/evil.py", "x\n", limits=DEFAULT_LIMITS)
    filesystem.write_file(workspace, "plugin/other.txt", "fine\n", limits=DEFAULT_LIMITS)


def test_an_8dot3_alias_is_caught_on_the_real_path(repo: Path) -> None:
    """Only the package is protected, so no lexical rule matches the short name."""
    package = repo / "src" / "universal_ai_tools"
    short = short_name(package)
    workspace = Workspace(repo, trusted=TrustedPaths([(package, SERVER_CODE)]))
    before = snapshot(repo)

    with pytest.raises(ProtectedPathError):
        edit(workspace, f"src/{short}/gate.py")
    with pytest.raises(ProtectedPathError):
        filesystem.write_file(workspace, f"src/{short}/evil.py", "x\n", limits=DEFAULT_LIMITS)
    with pytest.raises(ProtectedPathError):
        workspace.make_directory(f"src/{short}/plugins")
    assert snapshot(repo) == before


def test_a_workspace_strictly_inside_the_server_code_is_all_protected(repo: Path) -> None:
    package = repo / "src" / "universal_ai_tools"
    inner = package / "plugins"
    inner.mkdir()
    workspace = Workspace(inner, trusted=TrustedPaths([(package, SERVER_CODE)]))

    with pytest.raises(ProtectedPathError):
        filesystem.write_file(workspace, "new.py", "x\n", limits=DEFAULT_LIMITS)
    with pytest.raises(ProtectedPathError):
        workspace.make_directory("sub")


# Locations that appear after the protection was set up.


def test_a_module_imported_later_is_protected_from_then_on(tmp_path: Path) -> None:
    import importlib.util

    root = tmp_path.resolve()
    workspace = Workspace(root, trusted=TrustedPaths.for_runtime())
    name = f"uat_late_{uuid.uuid4().hex}"
    write(root / "plugin" / name / "__init__.py", "VALUE = 1\n")
    filesystem.write_file(workspace, f"plugin/{name}/before.py", "x\n", limits=DEFAULT_LIMITS)

    spec = importlib.util.spec_from_file_location(name, root / "plugin" / name / "__init__.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        with pytest.raises(ProtectedPathError):
            filesystem.write_file(
                workspace, f"plugin/{name}/after.py", "x\n", limits=DEFAULT_LIMITS
            )
        with pytest.raises(ProtectedPathError):
            edit(workspace, f"plugin/{name}/__init__.py", "1", "2")
    finally:
        sys.modules.pop(name, None)


def test_a_directory_added_to_the_import_path_later_is_protected(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "late-site").mkdir()
    workspace = Workspace(root, trusted=TrustedPaths.for_runtime())
    filesystem.write_file(workspace, "late-site/before.py", "x\n", limits=DEFAULT_LIMITS)

    sys.path.append(str(root / "late-site"))
    try:
        with pytest.raises(ProtectedPathError):
            filesystem.write_file(workspace, "late-site/json.py", "x\n", limits=DEFAULT_LIMITS)
    finally:
        sys.path.remove(str(root / "late-site"))


def test_directories_a_pth_file_names_are_protected_before_they_exist(tmp_path: Path) -> None:
    """A .pth line names a directory Python adds to sys.path on its next start."""
    root = tmp_path.resolve()
    write(root / "site" / "extra.pth", "# a comment\n../future-src\nimport os\n")
    workspace = Workspace(root, trusted=TrustedPaths.for_runtime())
    sys.path.append(str(root / "site"))
    try:
        with pytest.raises(ProtectedPathError):
            workspace.make_directory("future-src")
        with pytest.raises(ProtectedPathError):
            filesystem.write_file(workspace, "site/sitecustomize.py", "x\n", limits=DEFAULT_LIMITS)
        # Neither the comment nor the line that runs code names a directory.
        protected = {entry.path for entry in workspace.trusted}
        assert normalize(root / "site" / "# a comment") not in protected
        assert normalize(root / "site" / "import os") not in protected
    finally:
        sys.path.remove(str(root / "site"))
    assert not (root / "future-src").exists()


def test_site_directories_pth_files_are_read_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import site

    root = tmp_path.resolve()
    write(root / "sitedir" / "editable.pth", f"{root / 'checkout' / 'src'}\n")
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(root / "sitedir")])
    workspace = Workspace(root, trusted=TrustedPaths.for_runtime())

    workspace.make_directory("checkout")  # above what the .pth file names: harmless
    with pytest.raises(ProtectedPathError):
        workspace.make_directory("checkout/src")
    assert not (root / "checkout" / "src").exists()


def test_a_setuptools_editable_finder_mapping_is_protected(tmp_path: Path) -> None:
    import types

    root = tmp_path.resolve()
    write(root / "elsewhere" / "demo" / "__init__.py", "")
    workspace = Workspace(root, trusted=TrustedPaths.for_runtime())
    name = f"__editable___demo_{uuid.uuid4().hex}_finder"
    finder = types.ModuleType(name)
    finder.MAPPING = {"demo": str(root / "elsewhere" / "demo")}  # type: ignore[attr-defined]
    sys.modules[name] = finder
    try:
        with pytest.raises(ProtectedPathError):
            filesystem.write_file(workspace, "elsewhere/demo/evil.py", "x\n", limits=DEFAULT_LIMITS)
    finally:
        sys.modules.pop(name, None)
    filesystem.write_file(workspace, "elsewhere/other.txt", "fine\n", limits=DEFAULT_LIMITS)


# Places from which Windows runs programs by itself: writing there is command execution.


@pytest.mark.skipif(sys.platform != "win32", reason="Windows autostart folders")
def test_the_startup_folders_and_powershell_profiles_are_protected() -> None:
    import os

    from universal_ai_tools.trusted import AUTOSTART, autostart_paths

    paths = {normalize(p) for p in autostart_paths()}
    startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    assert normalize(startup) in paths
    assert any(p.endswith("windowspowershell") for p in paths)
    protected = {e.path: e.reason for e in TrustedPaths.for_runtime()}
    for path in autostart_paths():
        assert protected[normalize(Path(path).resolve())] == AUTOSTART


def test_nothing_is_written_where_programs_start_by_themselves(tmp_path: Path) -> None:
    from universal_ai_tools.trusted import AUTOSTART

    root = tmp_path.resolve()
    (root / "Startup").mkdir()
    workspace = Workspace(root, trusted=TrustedPaths([(root / "Startup", AUTOSTART)]))

    with pytest.raises(ProtectedPathError, match="runs programs by itself"):
        filesystem.write_file(workspace, "Startup/run.cmd", "calc\n", limits=DEFAULT_LIMITS)
    with pytest.raises(ProtectedPathError):
        workspace.move("Startup", "elsewhere")
    assert list((root / "Startup").iterdir()) == []

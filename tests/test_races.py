"""Races against a real second process.

``tests/test_policy.py`` forces each race at an exact moment inside one process. These
tests use another process instead, in two ways:

* Deterministic: while a call holds a directory, another process tries to change it.
* Concurrent: ``race_attacker.py`` changes the workspace in a loop while the tools run.

A concurrent run can only show that a problem was not observed, never that it cannot
happen. So each run that can has a control: a naive check-then-open reader in the same loop,
against the same attacker. If the control never reads the outside file, the attacker was not
acting inside a check-to-use gap, the run shows nothing, and it does not pass.

The control's gap is deliberately widened by ``CONTROL_GAP_SECONDS``, because a directory swap
takes several steps and could not otherwise land inside it. So the control shows the attacker
is really changing the disk between a check and a use. It does not show that the attacker hit
the far smaller windows in the hardened code on any given run.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path

import pytest

from support import link_file, security_skip, write
from universal_ai_tools import workspace as workspace_module
from universal_ai_tools.config import ENV_WORKSPACE, load_config
from universal_ai_tools.filesystem import list_files, read_file, search_files, write_file
from universal_ai_tools.workspace import Workspace, WorkspaceError, is_link

INSIDE = "INSIDE"
OTHER = "OTHER"
OUTSIDE = "SYNTHETIC-OUTSIDE-CONTENT"
OUTSIDE_ONLY_NAME = "only-outside.txt"
ATTACKER = Path(__file__).with_name("race_attacker.py")

MINIMUM_SECONDS = 2.0
MAXIMUM_SECONDS = 20.0
CONTROL_GAP_SECONDS = 0.002

# "exchange" is an atomic swap, which only Linux offers.
LINK_KINDS = ["junction", "symlink"] if sys.platform == "win32" else ["symlink", "exchange"]
FILE_LINK_KINDS = ["symlink"] if sys.platform == "win32" else ["symlink", "exchange"]

# Runs in the other process. Exit status 3 means the operating system refused.
_TRY_OPERATION = """
import os, sys
operation, first, *rest = sys.argv[1:]
try:
    getattr(os, operation)(first, *rest)
except PermissionError:
    sys.exit(3)
"""
_TRY_READ = """
import sys
try:
    open(sys.argv[1], "rb").close()
except PermissionError:
    sys.exit(3)
"""
_SWAP_FOR_LINK = """
import os, sys
directory, moved, target = sys.argv[1:]
os.rename(directory, moved)
os.symlink(target, directory, target_is_directory=True)
"""
REFUSED = 3


def in_other_process(script: str, *arguments: Path | str) -> int:
    done = subprocess.run(
        [sys.executable, "-c", script, *map(str, arguments)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode in (0, REFUSED), done.stderr
    return done.returncode


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
    write(path / "a.txt", OUTSIDE)
    return path


# Deterministic: another process acts while a call holds a directory or file.


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handles pin the path")
def test_other_process_cannot_move_or_delete_pinned_directories(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "a" / "b" / "c.txt", INSIDE)
    target = root / "a" / "b"
    results: dict[str, int] = {}
    real_open = workspace_module._open_file

    def attempt_then_open(directory: workspace_module._Directory, name: str) -> int:
        results["rename parent"] = in_other_process(
            _TRY_OPERATION, "rename", target, root / "a" / "moved"
        )
        results["rename grandparent"] = in_other_process(
            _TRY_OPERATION, "rename", root / "a", root / "moved"
        )
        results["replace parent"] = in_other_process(
            _TRY_OPERATION, "replace", target, root / "a" / "moved"
        )
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", attempt_then_open)

    assert read_file(workspace, "a/b/c.txt").content == INSIDE
    assert results == dict.fromkeys(results, REFUSED)
    assert len(results) == 3
    # Control: with no call in progress, the same operation succeeds.
    assert in_other_process(_TRY_OPERATION, "rename", target, root / "a" / "moved") == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handles pin the path")
def test_other_process_cannot_delete_a_directory_being_listed(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "empty").mkdir()
    results: list[int] = []
    real_scan = workspace_module._scan_entries

    def attempt_then_scan(directory: workspace_module._Directory) -> list[os.DirEntry[str]]:
        results.append(in_other_process(_TRY_OPERATION, "rmdir", root / "empty"))
        return real_scan(directory)

    monkeypatch.setattr(workspace_module, "_scan_entries", attempt_then_scan)

    assert list_files(workspace, "empty").entries == []
    assert results == [REFUSED]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes")
def test_other_process_cannot_touch_a_file_being_created(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results: dict[str, int] = {}
    real_write = workspace_module._write_all

    def attempt_then_write(fd: int, data: bytes) -> None:
        target = root / "new.txt"
        results["open"] = in_other_process(_TRY_READ, target)
        results["rename"] = in_other_process(_TRY_OPERATION, "rename", target, root / "moved.txt")
        results["delete"] = in_other_process(_TRY_OPERATION, "unlink", target)
        real_write(fd, data)

    monkeypatch.setattr(workspace_module, "_write_all", attempt_then_write)

    write_file(workspace, "new.txt", "data")

    assert results == {"open": REFUSED, "rename": REFUSED, "delete": REFUSED}
    assert (root / "new.txt").read_bytes() == b"data"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX allows renaming an open directory")
def test_other_process_swap_cannot_redirect_a_pinned_read(
    workspace: Workspace, root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The swap really happens; the read still reaches the directory that was checked."""
    write(root / "sub" / "a.txt", INSIDE)
    real_open = workspace_module._open_file

    def swap_then_open(directory: workspace_module._Directory, name: str) -> int:
        assert in_other_process(_SWAP_FOR_LINK, root / "sub", root / "sub-real", outside) == 0
        return real_open(directory, name)

    monkeypatch.setattr(workspace_module, "_open_file", swap_then_open)

    assert read_file(workspace, "sub/a.txt").content == INSIDE
    assert (root / "sub").is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX allows renaming an open directory")
def test_other_process_swap_cannot_redirect_a_pinned_listing(
    workspace: Workspace, root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "sub" / "inside.txt", INSIDE)
    real_scan = workspace_module._scan_entries

    def swap_then_scan(directory: workspace_module._Directory) -> list[os.DirEntry[str]]:
        assert in_other_process(_SWAP_FOR_LINK, root / "sub", root / "sub-real", outside) == 0
        return real_scan(directory)

    monkeypatch.setattr(workspace_module, "_scan_entries", swap_then_scan)

    assert [entry.path for entry in list_files(workspace, "sub").entries] == ["sub/inside.txt"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX allows renaming an open directory")
def test_other_process_swap_during_a_write_leaves_nothing_outside(
    workspace: Workspace, root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "sub").mkdir()
    real_create = workspace_module._create_new_file

    def swap_then_create(directory: workspace_module._Directory, name: str) -> object:
        assert in_other_process(_SWAP_FOR_LINK, root / "sub", root / "sub-real", outside) == 0
        return real_create(directory, name)

    monkeypatch.setattr(workspace_module, "_create_new_file", swap_then_create)

    with pytest.raises(WorkspaceError, match="directory changed while the file was being created"):
        write_file(workspace, "sub/new.txt", "data")

    assert sorted(os.listdir(outside)) == ["a.txt"]
    assert os.listdir(root / "sub-real") == []


# Concurrent: the attacker runs freely while the tools do.


@contextmanager
def attacker(scenario: str, root: Path, outside: Path, kind: str = "") -> Iterator[dict[str, int]]:
    """Run ``race_attacker.py`` for the duration of the block and collect its report."""
    stop = root.parent / "stop"
    process = subprocess.Popen(
        [sys.executable, str(ATTACKER), scenario, str(root), str(outside), str(stop), kind],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    report: dict[str, int] = {}
    try:
        yield report
    finally:
        stop.touch()
        try:
            output, errors = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    assert process.returncode == 0, errors
    report.update(json.loads(output))


def require_symlinks(tmp_path: Path) -> None:
    probe = tmp_path / "symlink-probe"
    link_file(probe, tmp_path)
    probe.unlink()


def running(control: Counter[str]) -> Iterator[int]:
    """Yield until the minimum time has passed and the control has hit the window."""
    started = time.monotonic()
    count = 0
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= MAXIMUM_SECONDS:
            return
        if elapsed >= MINIMUM_SECONDS and control["leaks"] > 0:
            return
        yield count
        count += 1


def naive_read(path: Path, root: Path, control: Counter[str], *, check_links: bool) -> None:
    """Check each component, then open by path: the design the server does not use."""
    try:
        if check_links:
            current = root
            for part in path.relative_to(root).parts:
                current = current / part
                if is_link(os.lstat(current)):
                    return
        time.sleep(CONTROL_GAP_SECONDS)
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    control["reads"] += 1
    if content == OUTSIDE:
        control["leaks"] += 1


def require_window_was_hit(control: Counter[str]) -> None:
    if control["leaks"] == 0:
        security_skip("the attacker never hit the window, so this run shows nothing")


def attempt[T](counts: Counter[str], name: str, call: Callable[[], T]) -> T | None:
    try:
        result = call()
    except WorkspaceError:
        counts[f"{name} refused"] += 1
        return None
    counts[f"{name} returned"] += 1
    return result


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_concurrent_parent_replacement_never_escapes(
    workspace: Workspace, root: Path, outside: Path, tmp_path: Path, kind: str
) -> None:
    if kind != "junction":
        require_symlinks(tmp_path)
    write(root / "sub" / "a.txt", INSIDE)
    write(outside / OUTSIDE_ONLY_NAME, OUTSIDE)
    counts: Counter[str] = Counter()
    control: Counter[str] = Counter()
    target = root / "sub" / "a.txt"

    with attacker("parent", root, outside, kind) as report:
        for number in running(control):
            read = attempt(counts, "read", lambda: read_file(workspace, "sub/a.txt"))
            assert read is None or OUTSIDE not in read.model_dump_json()
            listing = attempt(counts, "list", lambda: list_files(workspace, "sub"))
            assert listing is None or OUTSIDE_ONLY_NAME not in listing.model_dump_json()
            found = attempt(counts, "search", lambda: search_files(workspace, OUTSIDE[:9], "sub"))
            assert found is None or found.matches == []
            attempt(counts, "write", partial(write_file, workspace, f"sub/new{number}.txt", "x"))
            naive_read(target, root, control, check_links=True)

    print(f"RACE parent: {dict(counts)} control={dict(control)} attacker={report}")
    require_window_was_hit(control)
    assert sorted(os.listdir(outside)) == sorted(["a.txt", OUTSIDE_ONLY_NAME])
    assert (outside / "a.txt").read_text(encoding="utf-8") == OUTSIDE
    assert report["cycles"] > 0
    assert counts["read returned"] > 0
    assert counts["read refused"] > 0
    if sys.platform == "win32":
        # The kernel refused real attempts to rename the directory while a call held it.
        assert report["first_step_blocked"] > 0


@pytest.mark.parametrize("kind", FILE_LINK_KINDS)
def test_concurrent_file_symlink_substitution_never_escapes(
    workspace: Workspace, root: Path, outside: Path, tmp_path: Path, kind: str
) -> None:
    require_symlinks(tmp_path)
    write(root / "sub" / "a.txt", INSIDE)
    counts: Counter[str] = Counter()
    control: Counter[str] = Counter()
    target = root / "sub" / "a.txt"

    with attacker("file-symlink", root, outside, kind) as report:
        for _ in running(control):
            read = attempt(counts, "read", lambda: read_file(workspace, "sub/a.txt"))
            assert read is None or OUTSIDE not in read.model_dump_json()
            found = attempt(counts, "search", lambda: search_files(workspace, OUTSIDE[:9], "sub"))
            assert found is None or found.matches == []
            naive_read(target, root, control, check_links=True)

    print(f"RACE file-symlink: {dict(counts)} control={dict(control)} attacker={report}")
    require_window_was_hit(control)
    assert report["cycles"] > 0
    assert counts["read returned"] > 0
    assert counts["read refused"] > 0


def test_concurrent_hard_link_substitution_never_escapes(
    workspace: Workspace, root: Path, outside: Path
) -> None:
    write(root / "sub" / "a.txt", INSIDE)
    counts: Counter[str] = Counter()
    control: Counter[str] = Counter()
    target = root / "sub" / "a.txt"

    with attacker("file-hard-link", root, outside) as report:
        for _ in running(control):
            read = attempt(counts, "read", lambda: read_file(workspace, "sub/a.txt"))
            assert read is None or OUTSIDE not in read.model_dump_json()
            found = attempt(counts, "search", lambda: search_files(workspace, OUTSIDE[:9], "sub"))
            assert found is None or found.matches == []
            naive_read(target, root, control, check_links=False)

    print(f"RACE hard-link: {dict(counts)} control={dict(control)} attacker={report}")
    require_window_was_hit(control)
    assert report["cycles"] > 0
    assert counts["read returned"] > 0
    assert counts["read refused"] > 0


def test_concurrent_file_replacement_returns_one_whole_file(
    workspace: Workspace, root: Path, outside: Path
) -> None:
    """There is no leak to control for here: the point is that a read is never torn."""
    write(root / "sub" / "a.txt", INSIDE)
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    deadline = time.monotonic() + MINIMUM_SECONDS

    with attacker("file-replace", root, outside) as report:
        while time.monotonic() < deadline:
            read = attempt(counts, "read", lambda: read_file(workspace, "sub/a.txt"))
            if read is not None:
                seen.add(read.content)

    assert seen <= {INSIDE, OTHER}
    assert seen
    assert report["cycles"] > 0
    assert stat.S_ISREG(os.lstat(root / "sub" / "a.txt").st_mode)

"""A separate process that keeps changing a workspace while the race tests use it.

``tests/test_races.py`` starts this as a real child process, so the changes happen
concurrently with the code under test and not at a moment a test chose. It repeats one
scenario until the stop file appears, then prints how many cycles it finished and how many
steps the operating system refused. Pytest does not collect this file.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path


def make_directory_link(link: Path, target: Path, kind: str) -> None:
    if sys.platform == "win32" and kind == "junction":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def exchange(first: Path, second: Path) -> None:
    """Swap two names atomically, with Linux ``renameat2(RENAME_EXCHANGE)``.

    A swap in several steps is far slower than a check-to-use window. This one has no step at
    which neither name exists, so it can land inside a window of a few instructions.
    """
    current_directory, rename_exchange = -100, 2
    libc = ctypes.CDLL(None, use_errno=True)
    outcome = libc.renameat2(
        current_directory,
        os.fsencode(first),
        current_directory,
        os.fsencode(second),
        rename_exchange,
    )
    if outcome != 0:
        raise OSError(ctypes.get_errno(), "renameat2")


def parent_steps(root: Path, outside: Path, kind: str) -> list[Callable[[], object]]:
    """Replace the directory ``sub`` with a link to the outside, then put it back."""
    sub = root / "sub"
    moved = root / "sub-real"
    decoy = root / "decoy"
    # Built once and moved by rename. Creating a junction is not atomic: it starts as a plain
    # directory, which a failed attempt leaves behind for the tools to write into.
    make_directory_link(decoy, outside, kind)
    if kind == "exchange":
        return [lambda: exchange(sub, decoy)]
    return [
        lambda: sub.rename(moved),
        lambda: decoy.rename(sub),
        lambda: sub.rename(decoy),
        lambda: moved.rename(sub),
    ]


def file_symlink_steps(root: Path, outside: Path, kind: str) -> list[Callable[[], object]]:
    """Replace the file ``sub/a.txt`` with a symlink to the outside, then restore it."""
    target = root / "sub" / "a.txt"
    if kind == "exchange":
        decoy = root / "sub" / "decoy"
        decoy.symlink_to(outside / "a.txt")
        return [lambda: exchange(target, decoy)]
    return [
        target.unlink,
        lambda: target.symlink_to(outside / "a.txt"),
        target.unlink,
        lambda: _write_new(target, b"INSIDE"),
    ]


def file_hard_link_steps(root: Path, outside: Path, _kind: str) -> list[Callable[[], object]]:
    """Replace the file ``sub/a.txt`` with a hard link to the outside, then restore it."""
    target = root / "sub" / "a.txt"
    return [
        target.unlink,
        lambda: os.link(outside / "a.txt", target),
        target.unlink,
        lambda: _write_new(target, b"INSIDE"),
    ]


def file_replace_steps(root: Path, _outside: Path, _kind: str) -> list[Callable[[], object]]:
    """Atomically replace the file ``sub/a.txt`` with a different file, back and forth."""
    target = root / "sub" / "a.txt"
    spare = root / "spare.tmp"
    return [
        lambda: _write_new(spare, b"OTHER"),
        lambda: os.replace(spare, target),
        lambda: _write_new(spare, b"INSIDE"),
        lambda: os.replace(spare, target),
    ]


def _write_new(path: Path, data: bytes) -> None:
    with open(path, "xb") as handle:
        handle.write(data)


SCENARIOS = {
    "parent": parent_steps,
    "file-symlink": file_symlink_steps,
    "file-hard-link": file_hard_link_steps,
    "file-replace": file_replace_steps,
}


def main() -> int:
    scenario, root, outside, stop, kind = sys.argv[1:6]
    steps = SCENARIOS[scenario](Path(root), Path(outside), kind)
    stop_file = Path(stop)
    report = {"cycles": 0, "blocked": 0, "first_step_blocked": 0}
    while not stop_file.exists():
        for index, step in enumerate(steps):
            # The step is retried, because the sequence only makes sense in order.
            while True:
                try:
                    step()
                except OSError:
                    report["blocked"] += 1
                    if index == 0:
                        report["first_step_blocked"] += 1
                    if stop_file.exists():
                        print(json.dumps(report))
                        return 0
                else:
                    break
        report["cycles"] += 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

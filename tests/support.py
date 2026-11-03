"""Helpers shared by the filesystem security tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

import pytest

# Set in CI. A security test that cannot run on the platform it covers is then a failure.
STRICT_ENV = "UNIVERSAL_AI_TOOLS_STRICT_SECURITY_TESTS"


def security_skip(reason: str) -> NoReturn:
    """Stop a security test that cannot run here: a failure in strict mode, else a loud skip."""
    if os.environ.get(STRICT_ENV) == "1":
        pytest.fail(f"security test could not run: {reason}")
    pytest.skip(f"SECURITY COVERAGE MISSING: {reason}")


def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_bytes(content.encode("utf-8"))
    return path


def link_directory(link: Path, target: Path) -> None:
    """Create a directory symlink, or a junction on Windows without symlink privilege."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform == "win32":
            junction(link, target)
        else:
            security_skip("cannot create symlinks on this system")


def junction(link: Path, target: Path) -> None:
    """Create a Windows junction, which needs no privilege."""
    if sys.platform != "win32":
        pytest.skip("junctions are a Windows feature")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def link_file(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        security_skip("cannot create file symlinks here (Windows needs a privilege)")


def short_name(path: Path) -> str:
    """Return the Windows 8.3 short name of an existing path."""
    if sys.platform != "win32":
        pytest.skip("8.3 short names are a Windows feature")
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        security_skip("cannot query 8.3 short names here")
    name = Path(buffer.value).name
    if name.casefold() == path.name.casefold():
        security_skip("8.3 short names are disabled on this volume")
    return name

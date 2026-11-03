"""The filesystem policy: the one place that decides what may be accessed.

Every filesystem operation asks this module, so a rule cannot differ between tools:

* ``refusal`` answers for one path and one ``Operation``.
* ``is_excluded_directory_name`` says which directories a recursive walk stays out of.
* ``escape_text`` and ``escape_line`` make names and file content safe to return.

These are pure functions of path components. Nothing here touches the filesystem.
``Workspace`` applies ``refusal`` twice: to the requested path, before the filesystem
is touched, and to the real path of what it opened, which is what catches aliases such
as Windows 8.3 short names. Whatever exports workspace files later, such as a sandbox
snapshot, must do the same with ``Operation.READ`` rather than define its own rules.

The name rules are a safety net, not a secrets scanner. A secret stored under any other
name is an ordinary file, and they refuse some harmless files, such as a ``test.env``
or a registry-only ``.npmrc``. Refusing those is accepted; see docs/architecture.md.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import Enum


class Operation(Enum):
    """What a caller wants to do with a path."""

    LIST = "list"
    """Enumerate a directory. Names and sizes are visible; content is not."""

    READ = "read"
    """Return file content: ``read_file``, ``search_files``, any future exporter."""

    CREATE = "create"
    """Create a new file: ``write_file``."""


# Directories no operation may enter, at any depth. A file with one of these names is
# refused too: a .git file redirects Git to another repository.
SENSITIVE_DIRECTORY_NAMES = frozenset({".ssh", ".gnupg", ".aws", ".docker", ".kube", ".azure"})
METADATA_NAMES = frozenset({".git", ".hg", ".svn"})

# Generated or vendored directories. A recursive walk stays out of them, and nothing may
# be created in them, but a caller that names one can still list and read it.
_GENERATED_DIRECTORY_NAMES = frozenset(
    {
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".cache",
        ".idea",
    }
)
_GENERATED_DIRECTORY_SUFFIXES = (".egg-info",)

# Environment files. ``env.py`` (Alembic) matches none of these rules, and a name ending
# in a template suffix is exempt from the family, so .env.example stays readable.
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")
_ENV_PREFIXES = (".env.", ".env-", ".env_")
_ENV_STAGES = frozenset(
    {
        "ci",
        "dev",
        "development",
        "local",
        "preview",
        "prod",
        "production",
        "qa",
        "stage",
        "staging",
        "test",
        "testing",
        "uat",
    }
)

_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".env~",
        ".envrc",
        ".netrc",
        "_netrc",
        ".pgpass",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        ".dockercfg",
        ".htpasswd",
        "credentials",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "secrets.toml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_SENSITIVE_FILE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".ppk",
    ".jks",
    ".keystore",
    ".kdbx",
    ".tfstate",
    ".tfvars",
)

_SENSITIVE_FILE_REFUSALS = {
    Operation.READ: "reading sensitive files is not allowed",
    Operation.CREATE: "writing sensitive files is not allowed",
}

# Characters that can change what a reader sees or a terminal does, and that no text
# needs to carry literally: C0 and C1 controls except tab and line endings, DEL, line and
# paragraph separators, bidirectional overrides, lone surrogates (undecodable file names),
# and invisible tag characters. U+FEFF and joiners stay; ordinary scripts and emoji use them.
_UNSAFE = (
    r"\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    r"\u2028\u2029\u202a-\u202e\u2066-\u2069"
    r"\ud800-\udfff\U000e0000-\U000e007f"
)
_UNSAFE_IN_TEXT = re.compile(f"[{_UNSAFE}]")
_UNSAFE_IN_LINE = re.compile(rf"[\n\r{_UNSAFE}]")


def _fold(name: str) -> str:
    # Windows ignores trailing dots and spaces, so ".git." names .git on such a volume.
    return name.rstrip(". ").casefold()


def is_sensitive_file_name(name: str) -> bool:
    """Return whether ``name`` looks like a credential or secrets file."""
    folded = _fold(name)
    if folded in _SENSITIVE_FILE_NAMES or folded.endswith(_SENSITIVE_FILE_SUFFIXES):
        return True
    if folded.endswith(_ENV_TEMPLATE_SUFFIXES):
        return False
    return (
        folded.endswith(".env")
        or folded.startswith(_ENV_PREFIXES)
        or folded.removeprefix("env.") in _ENV_STAGES
    )


def is_excluded_directory_name(name: str) -> bool:
    """Return whether a recursive walk must stay out of a directory with this name."""
    folded = _fold(name)
    return (
        folded in SENSITIVE_DIRECTORY_NAMES
        or folded in METADATA_NAMES
        or folded in _GENERATED_DIRECTORY_NAMES
        or folded.endswith(_GENERATED_DIRECTORY_SUFFIXES)
    )


def refusal(parts: Sequence[str], operation: Operation) -> str | None:
    """Return why ``operation`` may not touch this path, or ``None`` if it may.

    Args:
        parts: Path components relative to the workspace root.
        operation: What the caller wants to do.
    """
    for part in parts:
        folded = _fold(part)
        if folded in SENSITIVE_DIRECTORY_NAMES:
            return "access to sensitive directories is not allowed"
        if folded in METADATA_NAMES:
            return "access to repository metadata is not allowed"
    if operation is Operation.LIST or not parts:
        return None
    if is_sensitive_file_name(parts[-1]):
        return _SENSITIVE_FILE_REFUSALS[operation]
    if operation is Operation.CREATE and any(is_excluded_directory_name(part) for part in parts):
        return "writing to excluded directories is not allowed"
    return None


def escape_text(text: str) -> tuple[str, int]:
    """Replace unsafe characters in multi-line text with visible escapes such as ``\\x1b``.

    Tabs and line endings are kept. Returns the text and how many characters changed,
    which is how a reader tells an escape from the same characters in the original.
    """
    return _UNSAFE_IN_TEXT.subn(_escaped, text)


def escape_line(text: str) -> str:
    """Escape unsafe characters in a value that must stay on one line, such as a name."""
    return _UNSAFE_IN_LINE.sub(_escaped, text)


def _escaped(found: re.Match[str]) -> str:
    code = ord(found.group())
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"

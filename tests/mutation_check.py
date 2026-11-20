"""Mutation check for the filesystem and process safeguards.

Run it directly: ``python tests/mutation_check.py`` (or name source files, such as
``python tests/mutation_check.py gate.py terminal.py``, to run only their mutants). It
removes one safeguard at a time from a scratch copy of the source and requires the tests to fail, which shows a test
really depends on that safeguard. It covers the safeguards listed here, chosen by hand,
so a clean run says nothing about code with no entry. Pytest does not collect this file.

Each mutant is classified by how the first failing test failed: an assertion, an expected
exception that did not occur (``pytest.raises``), or an exception nothing expected. A timeout is
not a kill: it is reported on its own and fails the run, as do survivors and mutants that could
not be tested. A test that needs to detect a hang must end by itself and fail.

A mutant whose ``old`` text no longer matches is an error, so the list cannot go stale.
Each mutant names the platforms where its safeguard is reachable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

REPOSITORY = Path(__file__).resolve().parent.parent
POLICY = "policy.py"
WORKSPACE = "workspace.py"
FILESYSTEM = "filesystem.py"
CONFIG = "config.py"
SERVER = "server.py"
TOOLS = "tools.py"
GATE = "gate.py"
APPROVAL = "approval.py"
SETTINGS = "settings.py"
TERMINAL = "terminal.py"
FILE_ACTIONS = "file_actions.py"
DESKTOP = "desktop/tools.py"
AUDIT = "audit.py"
CLIENTS = "clients.py"
CLI = "cli.py"
TRUSTED = "trusted.py"
GUARD = "guard.py"
STOP = "desktop/stop.py"
GRANTS = "desktop/grants.py"
LAUNCH = "desktop/launch.py"
SANDBOX = "sandbox.py"
ALL_PLATFORMS = ("win32", "linux")
TIMEOUT_SECONDS = 120

KILLED_BY_ASSERTION = "killed by assertion"
KILLED_BY_EXPECTED_EXCEPTION = "killed by expected exception"
KILLED_BY_UNEXPECTED_EXCEPTION = "killed by unexpected exception"
TIMED_OUT = "timed out"
SURVIVED = "survived"
COULD_NOT_TEST = "could not be tested"
OUTCOMES = (
    KILLED_BY_ASSERTION,
    KILLED_BY_EXPECTED_EXCEPTION,
    KILLED_BY_UNEXPECTED_EXCEPTION,
    TIMED_OUT,
    SURVIVED,
    COULD_NOT_TEST,
)

POLICY_TESTS = ("tests/test_policy.py",)
FILESYSTEM_TESTS = ("tests/test_policy.py", "tests/test_filesystem.py")
RACE_TESTS = (
    "tests/test_races.py::test_concurrent_parent_replacement_never_escapes",
    "tests/test_races.py::test_concurrent_parent_replacement_is_blocked_by_the_listing_alone",
)
REGISTRY_TESTS = ("tests/test_registry.py",)
SURFACE_TESTS = ("tests/test_mcp_surface.py", "tests/test_server.py")
GATE_TESTS = ("tests/test_gate.py", "tests/test_file_mode_server.py")
SETTINGS_TESTS = ("tests/test_settings.py",)
TERMINAL_TESTS = ("tests/test_terminal.py", "tests/test_no_subprocess.py")
FILE_ACTION_TESTS = ("tests/test_file_actions.py",)
DESKTOP_TESTS = ("tests/test_desktop.py", "tests/test_file_mode_server.py")
CLIENT_TESTS = ("tests/test_clients.py",)
APPROVAL_TESTS = ("tests/test_approval_integrity.py",)
SELF_MODIFICATION_TESTS = ("tests/test_self_modification.py",)
AUDIT_TESTS = ("tests/test_audit_completeness.py", "tests/test_gate.py")
FAIL_CLOSED_TESTS = ("tests/test_fail_closed.py",)
GUARD_TESTS = ("tests/test_guard.py",)
CONTROL_TESTS = ("tests/test_desktop_control.py", "tests/test_desktop.py")
GRANT_TESTS = ("tests/test_grants.py",)
READING_TESTS = ("tests/test_desktop_reading.py",)
SANDBOX_TESTS = ("tests/test_sandbox.py",)
REVIEW_TESTS = ("tests/test_review_fixes.py",)
GUARD_SETUP_TESTS = ("tests/test_setup.py",)


@dataclass(frozen=True)
class Mutant:
    name: str
    file: str
    old: str
    new: str
    tests: tuple[str, ...] = FILESYSTEM_TESTS
    platforms: tuple[str, ...] = ALL_PLATFORMS
    needs_symlinks: bool = False
    """Only a real file symlink can reach this safeguard on Windows."""
    also: tuple[tuple[str, str], ...] = ()
    """Further replacements, for two layers that each catch what the other misses."""


def _without_literal(values: list[str], file: str = POLICY) -> list[Mutant]:
    """One mutant per quoted name, each dropping that name from its table."""
    return [
        Mutant(f"drop {value}", file, f'        "{value}",\n', "", POLICY_TESTS) for value in values
    ]


MUTANTS: list[Mutant] = [
    # Names and tables.
    *_without_literal(
        [
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
        ]
    ),
    *[
        Mutant(f"drop {suffix}", POLICY, f'    "{suffix}",\n', "", POLICY_TESTS)
        for suffix in (
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
    ],
    *[
        Mutant(
            f"drop {name}",
            POLICY,
            'SENSITIVE_DIRECTORY_NAMES = frozenset({".ssh", ".gnupg", ".aws", ".docker", '
            '".kube", ".azure"})',
            "SENSITIVE_DIRECTORY_NAMES = frozenset({"
            + ", ".join(
                f'"{item}"'
                for item in (".ssh", ".gnupg", ".aws", ".docker", ".kube", ".azure")
                if item != name
            )
            + "})",
            POLICY_TESTS,
        )
        for name in (".ssh", ".gnupg", ".aws", ".docker", ".kube", ".azure")
    ],
    *[
        Mutant(
            f"drop {name}",
            POLICY,
            'METADATA_NAMES = frozenset({".git", ".hg", ".svn"})',
            "METADATA_NAMES = frozenset({"
            + ", ".join(f'"{item}"' for item in (".git", ".hg", ".svn") if item != name)
            + "})",
            POLICY_TESTS,
        )
        for name in (".git", ".hg", ".svn")
    ],
    Mutant(
        "no .env prefixes",
        POLICY,
        '_ENV_PREFIXES = (".env.", ".env-", ".env_")',
        '_ENV_PREFIXES = (".env.", ".env-")',
        POLICY_TESTS,
    ),
    Mutant(
        "no .env- prefix",
        POLICY,
        '_ENV_PREFIXES = (".env.", ".env-", ".env_")',
        '_ENV_PREFIXES = (".env.", ".env_")',
        POLICY_TESTS,
    ),
    Mutant(
        "no *.env rule",
        POLICY,
        'folded.endswith(".env")\n        or folded.startswith',
        'folded == ".env"\n        or folded.startswith',
        POLICY_TESTS,
    ),
    Mutant(
        "no env.<stage> rule",
        POLICY,
        '        or folded.removeprefix("env.") in _ENV_STAGES\n',
        "",
        POLICY_TESTS,
    ),
    Mutant(
        "no template exception",
        POLICY,
        '_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")',
        '_ENV_TEMPLATE_SUFFIXES = ("\\0",)',
        POLICY_TESTS,
    ),
    Mutant(
        "template exception covers credential names",
        POLICY,
        "    if folded in _SENSITIVE_FILE_NAMES or folded.endswith(_SENSITIVE_FILE_SUFFIXES):\n"
        "        return True\n"
        "    if folded.endswith(_ENV_TEMPLATE_SUFFIXES):\n"
        "        return False\n",
        '    if folded.endswith(_ENV_TEMPLATE_SUFFIXES + ("s.yaml",)):\n'
        "        return False\n"
        "    if folded in _SENSITIVE_FILE_NAMES or folded.endswith(_SENSITIVE_FILE_SUFFIXES):\n"
        "        return True\n",
        POLICY_TESTS,
    ),
    Mutant(
        "names are case-sensitive",
        POLICY,
        'return name.rstrip(". ").casefold()',
        'return name.rstrip(". ")',
        POLICY_TESTS,
    ),
    Mutant(
        "trailing characters hide a name",
        POLICY,
        'return name.rstrip(". ").casefold()',
        "return name.casefold()",
        POLICY_TESTS,
    ),
    # One rule, every operation.
    Mutant(
        "metadata is not denied",
        POLICY,
        '            return "access to repository metadata is not allowed"\n',
        "            continue\n",
    ),
    Mutant(
        "sensitive directories are not denied",
        POLICY,
        '            return "access to sensitive directories is not allowed"\n',
        "            continue\n",
    ),
    Mutant(
        "sensitive names are not refused",
        POLICY,
        "    if is_sensitive_file_name(parts[-1]):\n",
        "    if False:\n",
    ),
    Mutant(
        "generated directories are writable",
        POLICY,
        "if operation in _CHANGING and any(",
        "if False and any(",
    ),
    Mutant(
        "walk enters metadata",
        POLICY,
        "        or folded in METADATA_NAMES\n",
        "",
    ),
    Mutant(
        "walk enters sensitive directories",
        POLICY,
        "        folded in SENSITIVE_DIRECTORY_NAMES\n        or folded",
        "        folded",
    ),
    Mutant(
        "search ignores the policy",
        FILESYSTEM,
        "if refusal(item.parts, Operation.READ) is not None:",
        "if False:",
    ),
    Mutant(
        "read_file skips the lexical check",
        WORKSPACE,
        "        _enforce(parts, operation, relative)\n        if not parts:",
        "        if not parts:",
    ),
    Mutant(
        "write skips the lexical check",
        WORKSPACE,
        "        _enforce(parts, Operation.CREATE, relative)\n"
        "        self._require_unprotected(self._lexical(parts), relative)\n\n"
        "        name = parts[-1]\n",
        "        self._require_unprotected(self._lexical(parts), relative)\n\n"
        "        name = parts[-1]\n",
    ),
    # Links and identity.
    Mutant(
        "hard links are read",
        WORKSPACE,
        "if opened.st_nlink > 1:",
        "if False:",
    ),
    Mutant(
        "no identity check",
        WORKSPACE,
        "    return expected.st_ino == 0 or os.path.samestat(expected, opened)",
        "    return True",
    ),
    Mutant(
        "real path is not checked after opening",
        WORKSPACE,
        "            if real is not None:\n"
        "                self._require_allowed(real, relative, operation)\n",
        "",
    ),
    Mutant(
        "real path may leave the root",
        WORKSPACE,
        "            inside = Path(real).relative_to(self._root).parts\n"
        "        except ValueError:\n"
        "            raise OutsideWorkspaceError(\n"
        "                f\"path resolves outside the workspace: '{relative}'\"\n"
        "            ) from None\n",
        "            inside = Path(real).relative_to(self._root).parts\n"
        "        except ValueError:\n"
        "            return\n",
    ),
    Mutant(
        "directory real path is not checked",
        WORKSPACE,
        "            if real is not None:\n"
        "                self._require_allowed(real, relative, Operation.LIST)\n",
        "",
        platforms=("win32",),
    ),
    Mutant(
        "parent real path is not checked before creating",
        WORKSPACE,
        "        if real_parent is not None:\n"
        "            # The real path also catches aliases such as the 8.3 short name GIT~1 for "
        ".git.\n"
        "            self._require_allowed(os.path.join(real_parent, name), relative, "
        "Operation.CREATE)\n",
        "",
        platforms=("win32",),
    ),
    Mutant(
        "created file location is not checked",
        WORKSPACE,
        "if real_parent is not None and Path(real) != Path(real_parent) / name:",
        "if False:",
        platforms=("linux",),
    ),
    Mutant(
        "file open follows symlinks (POSIX)",
        WORKSPACE,
        "        | os.O_NOFOLLOW\n        # Never block",
        "        # Never block",
        platforms=("linux",),
    ),
    Mutant(
        "file open can block on a FIFO (POSIX)",
        WORKSPACE,
        "        | os.O_NONBLOCK\n",
        "",
        platforms=("linux",),
    ),
    Mutant(
        "directory walk follows symlinks and skips the identity check (POSIX)",
        WORKSPACE,
        "    _DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW\n",
        "    _DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY\n",
        platforms=("linux",),
        also=(("if not os.path.samestat(os.fstat(fd), st):", "if False:"),),
    ),
    Mutant(
        "directory link is not inspected (POSIX)",
        WORKSPACE,
        "            st = self.lstat(name)\n"
        "            if is_link(st):\n"
        '                raise LinkRefusedError(f"refusing to follow symlink or junction: '
        "'{prefix}'\")\n",
        "            st = self.lstat(name)\n",
        platforms=("linux",),
    ),
    Mutant(
        "directory link is not inspected (Windows)",
        WORKSPACE,
        "                if _is_redirect(info.attributes, info.reparse_tag):\n"
        '                    raise LinkRefusedError(f"refusing to follow symlink or junction: '
        "'{prefix}'\")\n",
        "",
        platforms=("win32",),
    ),
    Mutant(
        "file redirect is not inspected (Windows)",
        WORKSPACE,
        "            if _is_redirect(info.attributes, info.reparse_tag):\n"
        '                raise OSError(errno.ELOOP, "refusing to follow a link")\n',
        "",
        platforms=("win32",),
    ),
    Mutant(
        "file open follows reparse points (Windows)",
        WORKSPACE,
        "            _OPEN_EXISTING,\n"
        "            _FILE_FLAG_OPEN_REPARSE_POINT,\n"
        "        )\n"
        "        try:\n"
        "            info = _attributes_of(handle)\n"
        "            if _is_redirect(info.attributes, info.reparse_tag):\n"
        '                raise OSError(errno.ELOOP, "refusing to follow a link")\n',
        "            _OPEN_EXISTING,\n"
        "            0,\n"
        "        )\n"
        "        try:\n"
        "            info = _attributes_of(handle)\n"
        "            if _is_redirect(info.attributes, info.reparse_tag):\n"
        '                raise OSError(errno.ELOOP, "refusing to follow a link")\n',
        platforms=("win32",),
        needs_symlinks=True,
    ),
    Mutant(
        "concurrent attacker reaches a walk that follows links (POSIX)",
        WORKSPACE,
        "    _DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW\n",
        "    _DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY\n",
        RACE_TESTS,
        platforms=("linux",),
        also=(
            ("if not os.path.samestat(os.fstat(fd), st):", "if False:"),
            (
                "            if real is not None:\n"
                "                self._require_allowed(real, relative, operation)\n",
                "",
            ),
            (
                "            if real is not None:\n"
                "                self._require_allowed(real, relative, Operation.LIST)\n",
                "",
            ),
        ),
    ),
    Mutant(
        "concurrent attacker reaches unpinned directories (Windows)",
        WORKSPACE,
        "            _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,\n",
        "            _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,\n",
        RACE_TESTS,
        platforms=("win32",),
    ),
    Mutant(
        "handles do not pin (Windows)",
        WORKSPACE,
        "            _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,\n",
        "            _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,\n",
        platforms=("win32",),
    ),
    Mutant(
        "directory handles allow deletion (Windows)",
        WORKSPACE,
        "_SYNCHRONIZE,\n            _FILE_SHARE_READ_WRITE,\n",
        "_SYNCHRONIZE,\n            _FILE_SHARE_READ_WRITE | 0x4,\n",
        platforms=("win32",),
    ),
    Mutant(
        "handles are not released (Windows)",
        WORKSPACE,
        "            while self._handles:\n                _close_handle(self._handles.pop())\n",
        "            self._handles.clear()\n",
        platforms=("win32",),
    ),
    Mutant(
        "descriptor is not released (POSIX)",
        WORKSPACE,
        "        def close(self) -> None:\n            os.close(self.fd)\n\n"
        "    def _open_root(root: Path)",
        "        def close(self) -> None:\n            pass\n\n    def _open_root(root: Path)",
        platforms=("linux",),
    ),
    Mutant(
        "listing goes by path (POSIX)",
        WORKSPACE,
        "        with os.scandir(directory.fd) as iterator:\n",
        "        with os.scandir(os.readlink(f'/proc/self/fd/{directory.fd}').replace("
        "'/sub', '/sub', 1).removesuffix('-real')) as iterator:\n",
        platforms=("linux",),
    ),
    Mutant(
        "symlink loop error is not reported as a link",
        WORKSPACE,
        "    if exc.errno == errno.ELOOP:\n"
        '        return LinkRefusedError(f"refusing to follow symlink or junction: '
        "'{relative}'\")\n",
        "",
        platforms=("linux",),
    ),
    # Limits.
    Mutant(
        "path length is unlimited",
        WORKSPACE,
        "    if len(raw) > MAX_PATH_CHARS:\n",
        "    if False:\n",
    ),
    Mutant(
        "unencodable paths reach the OS",
        WORKSPACE,
        "    try:\n"
        "        os.fsencode(raw)\n"
        "    except UnicodeEncodeError:\n"
        '        raise InvalidPathError("path is not valid text for this filesystem") from None\n',
        "",
        platforms=("linux",),
    ),
    Mutant(
        "read ignores the size limit while reading",
        WORKSPACE,
        "        if len(data) > max_bytes:\n",
        "        if False:\n",
    ),
    Mutant(
        "read ignores the response limit",
        FILESYSTEM,
        "    if size > limits.max_response_bytes:\n",
        "    if False:\n",
    ),
    Mutant(
        "listing ignores the response limit",
        FILESYSTEM,
        "        if not budget.admit(entry):\n",
        "        if False:\n",
    ),
    Mutant(
        "search ignores the response limit",
        FILESYSTEM,
        "                if not budget.admit(match):\n",
        "                if False:\n",
    ),
    Mutant(
        "budget ignores the fixed part of a result",
        FILESYSTEM,
        "        self._remaining = limit - _json_size(envelope)\n",
        "        self._remaining = limit\n",
    ),
    Mutant(
        "budget ignores separators",
        FILESYSTEM,
        "        self._separator = 1\n",
        "        self._separator = 0\n",
    ),
    Mutant(
        "budget measures characters",
        FILESYSTEM,
        '    return len(model.model_dump_json().encode("utf-8"))\n',
        "    return len(model.model_dump_json())\n",
    ),
    Mutant(
        "listing envelope assumes no truncation",
        FILESYSTEM,
        '            truncation_reason="max_response_bytes",\n'
        "            skipped_directories=_LARGEST_COUNT,\n",
        "            truncation_reason=None,\n            skipped_directories=_LARGEST_COUNT,\n",
    ),
    Mutant(
        "search envelope assumes no truncation",
        FILESYSTEM,
        '            truncation_reason="max_response_bytes",\n            skipped=SearchSkips(\n',
        "            truncation_reason=None,\n            skipped=SearchSkips(\n",
    ),
    # Untrusted output.
    Mutant(
        "content is not escaped",
        FILESYSTEM,
        "    content, escaped = escape_text(_decode(data, target))\n",
        "    content, escaped = _decode(data, target), 0\n",
    ),
    Mutant(
        "snippets are not escaped",
        FILESYSTEM,
        "snippet=escape_line(\n"
        '                        _snippet(line.rstrip("\\r"), found, limits.max_snippet_chars)\n'
        "                    ),\n",
        'snippet=_snippet(line.rstrip("\\r"), found, limits.max_snippet_chars),\n',
    ),
    Mutant(
        "listed names are not escaped",
        FILESYSTEM,
        "            path=escape_line(item.relative),\n            type=kind,\n",
        "            path=item.relative,\n            type=kind,\n",
    ),
    Mutant(
        "matched names are not escaped",
        FILESYSTEM,
        "                    path=escape_line(item.relative),\n                    line=number,\n",
        "                    path=item.relative,\n                    line=number,\n",
    ),
    Mutant(
        "pattern is echoed raw",
        FILESYSTEM,
        "    shown_pattern = escape_line(pattern)\n",
        "    shown_pattern = pattern\n",
    ),
    Mutant(
        "bidi controls are not escaped",
        POLICY,
        r'r"\u2028\u2029\u202a-\u202e\u2066-\u2069"',
        r'r"\u2028\u2029"',
        POLICY_TESTS,
    ),
    Mutant(
        "tag characters are not escaped",
        POLICY,
        r'r"\ud800-\udfff\U000e0000-\U000e007f"',
        r'r"\ud800-\udfff"',
        POLICY_TESTS,
    ),
    Mutant(
        "surrogates are not escaped",
        POLICY,
        r'r"\ud800-\udfff\U000e0000-\U000e007f"',
        r'r"\U000e0000-\U000e007f"',
        POLICY_TESTS,
    ),
    Mutant(
        "C1 controls are not escaped",
        POLICY,
        r'r"\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"',
        r'r"\x00-\x08\x0b\x0c\x0e-\x1f\x7f"',
        POLICY_TESTS,
    ),
    Mutant(
        "refusals quote raw paths",
        SERVER,
        "raise ToolError(escape_line(str(exc))) from None",
        "raise ToolError(str(exc)) from None",
        ("tests/test_server.py",),
    ),
    Mutant(
        "results are not labelled untrusted",
        FILESYSTEM,
        '    return Field(\n        default="untrusted",\n',
        '    return Field(\n        default="trusted",\n',
    ),
    # Configuration and processes.
    Mutant(
        "removed settings are ignored",
        CONFIG,
        "    if problems:\n",
        "    if False:\n",
        ("tests/test_config.py", "tests/test_server.py"),
    ),
    Mutant(
        "a value of false is accepted",
        CONFIG,
        '        for name in REMOVED_SETTINGS\n        if source.get(name, "").strip()\n',
        '        for name in REMOVED_SETTINGS\n        if source.get(name, "").strip() not in ("", "false")\n',
        ("tests/test_config.py",),
    ),
    Mutant(
        "a tool call starts a process",
        TOOLS,
        "    return PlatformInfo(name=APP_NAME,",
        '    __import__("subprocess").run([__import__("sys").executable, "-c", "pass"], '
        "check=True)\n"
        "    return PlatformInfo(name=APP_NAME,",
        ("tests/test_no_subprocess.py",),
    ),
    # The tool registry.
    Mutant(
        "write_file is registered when disabled",
        TOOLS,
        "    if app.enable_write_tools:\n        specs.append(",
        "    if True:\n        specs.append(",
        (*REGISTRY_TESTS, *SURFACE_TESTS),
    ),
    Mutant(
        "the server ignores the write setting",
        SERVER,
        "    registry = build_registry(service, limits, trusted=trusted)\n",
        '    registry = build_registry(__import__("dataclasses").replace(service, '
        'app=service.app.model_copy(update={"enable_write_tools": True})), limits, '
        "trusted=trusted)\n",
        SURFACE_TESTS,
    ),
    Mutant(
        "the registry coerces arguments",
        TOOLS,
        "return self.input_model.model_validate(dict(arguments), strict=True)",
        "return self.input_model.model_validate(dict(arguments), strict=False)",
        REGISTRY_TESTS,
        also=(
            (
                "STRICT_INPUT = ConfigDict(strict=True,",
                "STRICT_INPUT = ConfigDict(strict=False,",
            ),
            ('if config.get("strict") is not True or', "if False and config.get('strict') or"),
        ),
    ),
    Mutant(
        "the registry ignores unexpected properties",
        TOOLS,
        'STRICT_INPUT = ConfigDict(strict=True, extra="forbid",',
        'STRICT_INPUT = ConfigDict(strict=True, extra="ignore",',
        REGISTRY_TESTS,
        also=(
            ('config.get("extra") != "forbid"', 'config.get("extra") not in ("forbid", "ignore")'),
        ),
    ),
    Mutant(
        "lax input models are accepted",
        TOOLS,
        '        if config.get("strict") is not True or config.get("extra") != "forbid":\n',
        "        if False:\n",
        REGISTRY_TESTS,
    ),
    Mutant(
        "risk and annotations may disagree",
        TOOLS,
        "        if self.annotations.read_only_hint is not read_only:\n",
        "        if False:\n",
        REGISTRY_TESTS,
    ),
    Mutant(
        "the registry runs another tool's handler",
        TOOLS,
        "        result: BaseModel = self.get(name).call(arguments)\n",
        "        self.get(name)\n"
        "        result: BaseModel = next(iter(self._tools.values())).call(arguments)\n",
        REGISTRY_TESTS,
    ),
    Mutant(
        "registry refusals are not escaped",
        TOOLS,
        "    return escape_line(text)\n",
        "    return text\n",
        REGISTRY_TESTS,
    ),
    Mutant(
        "MCP calls bypass the registry",
        TOOLS,
        "            return await anyio.to_thread.run_sync(self.invoke, name, arguments)\n",
        "            return await anyio.to_thread.run_sync(spec.call, arguments)\n",
        REGISTRY_TESTS,
    ),
    Mutant(
        "a tool description changes",
        TOOLS,
        "to confirm the server is connected and to check which version is running.",
        "to confirm the server is connected.",
        ("tests/test_mcp_surface.py",),
    ),
    # The gate: permission decisions and approval.
    Mutant(
        "consequential tools run without approval",
        GATE,
        "        if not self._service.file_mode or spec.risk is ToolRisk.READ_ONLY:\n",
        "        if True:\n",
        GATE_TESTS,
    ),
    Mutant(
        "observation counts as read-only",
        GATE,
        "        if not self._service.file_mode or spec.risk is ToolRisk.READ_ONLY:\n",
        "        if not self._service.file_mode or spec.risk in (ToolRisk.READ_ONLY, ToolRisk.OBSERVE):\n",
        GATE_TESTS,
    ),
    Mutant(
        "a refusal still runs the call",
        GATE,
        "            if outcome.decision is not ApprovalDecision.APPROVED:\n",
        "            if False:\n",
        GATE_TESTS,
    ),
    Mutant(
        "a session grant never expires",
        GATE,
        '        if policy == "once_per_session" and self._grants.get(spec.capability, 0) > now:\n',
        "        if self._grants.get(spec.capability, 0):\n",
        GATE_TESTS,
    ),
    Mutant(
        "a denial grants the session",
        GATE,
        '        if outcome.decision is ApprovalDecision.APPROVED and policy == "once_per_session":\n',
        '        if policy == "once_per_session":\n',
        GATE_TESTS,
    ),
    Mutant(
        "a deferred answer is not bound to its call",
        GATE,
        "            or pending.digest != digest\n",
        "",
        GATE_TESTS,
    ),
    Mutant(
        "a deferred answer can be replayed",
        GATE,
        "        pending = self._pending.pop(nonce, None) if isinstance(nonce, str) else None\n",
        "        pending = self._pending.get(nonce) if isinstance(nonce, str) else None\n",
        GATE_TESTS,
    ),
    Mutant(
        "silence counts as approval",
        GATE,
        "                decision = ApprovalDecision.TIMED_OUT\n",
        "                decision = ApprovalDecision.APPROVED\n",
        GATE_TESTS,
    ),
    Mutant(
        "an unauditable call runs",
        GATE,
        "                raise ToolCallError(\n"
        "                    f\"'{spec.name}' did not run because the audit log cannot be written ({exc})\"\n"
        "                ) from None\n",
        "                pass\n",
        GATE_TESTS,
    ),
    Mutant(
        "the client channel falls back to the desktop",
        GATE,
        '        if mode in ("auto", "desktop_dialog") and self._dialog_available():\n',
        "        if self._dialog_available():\n",
        GATE_TESTS,
    ),
    Mutant(
        "an approval form with the wrong value approves",
        APPROVAL,
        '    return ApprovalDecision.APPROVED if form.decision == "approve" else ApprovalDecision.DENIED\n',
        "    return ApprovalDecision.APPROVED\n",
        GATE_TESTS,
    ),
    Mutant(
        "an open approval form accepts extra fields",
        APPROVAL,
        '    model_config = ConfigDict(extra="forbid", strict=True)\n\n    decision',
        '    model_config = ConfigDict(extra="ignore", strict=True)\n\n    decision',
        GATE_TESTS,
    ),
    Mutant(
        "a dismissed dialog approves",
        APPROVAL,
        '        if answer == "yes" and not handle.closed:\n',
        '        if answer in ("yes", "dismissed"):\n',
        GATE_TESTS,
    ),
    # The config file.
    Mutant(
        "the config file may live in the workspace",
        SETTINGS,
        "    if inside(config_path):\n",
        "    if False:\n",
        SETTINGS_TESTS,
        also=(("    if inside(config_path.parent):\n", "    if False:\n"),),
    ),
    Mutant(
        "the audit log may live in the workspace",
        SETTINGS,
        "    if inside(audit_path):\n",
        "    if False:\n",
        SETTINGS_TESTS,
    ),
    Mutant(
        "the workspace may hold Claude's settings",
        SETTINGS,
        "        if inside(path):\n",
        "        if False:\n",
        SETTINGS_TESTS,
    ),
    Mutant(
        "the config file and variables may be mixed",
        SETTINGS,
        "    if mixed:\n",
        "    if False:\n",
        SETTINGS_TESTS,
    ),
    Mutant(
        "unknown config keys are ignored",
        SETTINGS,
        '_SECTION = ConfigDict(extra="forbid", strict=True, frozen=True)',
        '_SECTION = ConfigDict(extra="ignore", strict=True, frozen=True)',
        SETTINGS_TESTS,
    ),
    Mutant(
        "API agent settings are ignored",
        CONFIG,
        "        for name in REMOVED_AGENT_SETTINGS\n",
        "        for name in ()\n",
        SETTINGS_TESTS,
    ),
    # The terminal.
    Mutant(
        "blocked programs are allowed",
        TERMINAL,
        "        if candidate_name in BLOCKED_PROGRAMS:\n",
        "        if False:\n",
        TERMINAL_TESTS,
    ),
    Mutant(
        "programs in the workspace are allowed",
        TERMINAL,
        "    if real.is_relative_to(workspace_root) or found.is_relative_to(workspace_root):\n",
        "    if False:\n",
        TERMINAL_TESTS,
    ),
    Mutant(
        "PATH entries in the workspace are searched",
        TERMINAL,
        "            if base.resolve().is_relative_to(workspace_root):\n",
        "            if False:\n",
        TERMINAL_TESTS,
    ),
    Mutant(
        "the whole environment is inherited",
        TERMINAL,
        '    kept["NO_COLOR"] = "1"\n    return kept\n',
        "    return dict(environ)\n",
        TERMINAL_TESTS,
    ),
    Mutant(
        "output is not capped",
        TERMINAL,
        "                if room > 0:\n                    self.data += chunk[:room]\n",
        "                self.data += chunk\n",
        TERMINAL_TESTS,
    ),
    Mutant(
        "children outlive the command",
        TERMINAL,
        "            _kernel32.TerminateJobObject(job, 1)\n            _kernel32.CloseHandle(job)\n",
        "            pass\n",
        TERMINAL_TESTS,
        platforms=("win32",),
    ),
    Mutant(
        "the time limit is not enforced",
        TERMINAL,
        "                if time.monotonic() - started > timeout:\n",
        "                if False:\n",
        TERMINAL_TESTS,
    ),
    Mutant(
        "commands run concurrently",
        TERMINAL,
        "        if self._busy:\n",
        "        if False:\n",
        TERMINAL_TESTS,
    ),
    # File changes.
    Mutant(
        "edits change an ambiguous match",
        FILE_ACTIONS,
        "        if count != arguments.expected_replacements:\n",
        "        if count == 0:\n",
        FILE_ACTION_TESTS,
    ),
    Mutant(
        "edits follow the MODIFY policy only lexically",
        WORKSPACE,
        "            old = self._read_in(parent, target, max_read_bytes, Operation.MODIFY)\n",
        "            old = self._read_in(parent, target, max_read_bytes, Operation.READ)\n",
        FILE_ACTION_TESTS,
        platforms=("win32",),
        # The temporary file's real-path check under CREATE is the second layer: with only
        # the first removed, the edit is still refused there.
        also=(
            (
                "            # The real path also catches aliases such as the 8.3 short name GIT~1 for .git.\n"
                "            self._require_allowed(os.path.join(real_parent, name), relative, Operation.CREATE)\n",
                "            pass\n",
            ),
        ),
    ),
    Mutant(
        "moves overwrite",
        WORKSPACE,
        "                    self._require_new(destination_parent, destination_parts[-1], destination)\n",
        "",
        FILE_ACTION_TESTS,
        platforms=("linux",),
    ),
    Mutant(
        "a directory can be moved into itself",
        WORKSPACE,
        "        if _inside(destination_parts, source_parts):\n",
        "        if False:\n",
        (*FILE_ACTION_TESTS, *REVIEW_TESTS),
    ),
    Mutant(
        "a link can be moved",
        WORKSPACE,
        "            if is_link(st):\n                raise LinkRefusedError(f\"refusing to move a symlink or junction: '{source}'\")\n",
        "",
        FILE_ACTION_TESTS,
    ),
    Mutant(
        "MODIFY skips excluded directories",
        POLICY,
        "_CHANGING = frozenset({Operation.CREATE, Operation.MODIFY})",
        "_CHANGING = frozenset({Operation.CREATE})",
        FILE_ACTION_TESTS,
    ),
    # Screen and input.
    Mutant(
        "protected programs receive input",
        DESKTOP,
        "        if process in PROTECTED_PROCESSES:\n",
        "        if False:\n",
        DESKTOP_TESTS,
    ),
    Mutant(
        "elevated programs receive input",
        DESKTOP,
        "        if window.elevated:\n",
        "        if False:\n",
        DESKTOP_TESTS,
    ),
    Mutant(
        "input goes on while an approval dialog is open",
        DESKTOP,
        "        if self._backend.approval_dialog_open():\n",
        "        if False:\n",
        DESKTOP_TESTS,
    ),
    Mutant(
        "clicks land on whatever is under the pointer",
        DESKTOP,
        "        if under != window.window_id:\n",
        "        if False:\n",
        DESKTOP_TESTS,
    ),
    Mutant(
        "typing ignores a focus change",
        DESKTOP,
        "                elif self._backend.foreground_window() != window.window_id:\n",
        "                elif False:\n",
        DESKTOP_TESTS,
    ),
    Mutant(
        "typing that arrived wrong is reported as success",
        DESKTOP,
        "    return len(new) - len(old) == len(typed) and typed in new\n",
        "    return True\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "typing ignores an approval dialog that opens",
        DESKTOP,
        "                elif self._backend.approval_dialog_open():\n",
        "                elif False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "old screenshots stay valid",
        DESKTOP,
        "        if self._clock() - capture.taken > CAPTURE_TTL_SECONDS:\n",
        "        if False:\n",
        DESKTOP_TESTS,
    ),
    Mutant(
        "the Windows key is allowed",
        DESKTOP,
        "        if name in _REFUSED_KEYS:\n",
        "        if False:\n",
        DESKTOP_TESTS,
    ),
    Mutant(
        "typed text reaches the audit log",
        DESKTOP,
        '            lambda a: {"characters": len(a.text)},\n',
        '            lambda a: {"characters": len(a.text), "text": a.text},\n',
        DESKTOP_TESTS,
    ),
    # Client configuration.
    Mutant(
        "invalid Claude Desktop JSON is overwritten",
        CLIENTS,
        "        except ValueError as exc:\n            raise ClientConfigError(\n",
        "        except ValueError as exc:\n            data = {}\n            raise ClientConfigError(\n",
        CLIENT_TESTS,
        also=(
            (
                "    if before.strip():\n        try:\n",
                '    if before.strip() and before.lstrip().startswith("{"):\n        try:\n',
            ),
        ),
    ),
    Mutant(
        "a Desktop config changed meanwhile is overwritten",
        CLIENTS,
        '    if current != plan.before:\n        raise ClientConfigError(f"{plan.path} changed since the plan was made; run setup again")',
        '    if False:\n        raise ClientConfigError(f"{plan.path} changed since the plan was made; run setup again")',
        CLIENT_TESTS,
    ),
    # The approval shows the complete request, exactly.
    Mutant(
        "a long value is cut",
        APPROVAL,
        "    tokens = ['\"', *_tokens(shown, escaped=escaped), '\"']\n",
        "    tokens = ['\"', *_tokens(shown[:1200], escaped=escaped), '\"']\n",
        APPROVAL_TESTS,
    ),
    Mutant(
        "a long text is cut",
        APPROVAL,
        '    rows = text.split("\\n")\n',
        '    rows = text[:1200].split("\\n")\n',
        APPROVAL_TESTS,
    ),
    Mutant(
        "a request too large for the dialog is shown anyway",
        APPROVAL,
        "    if len(lines) > limits.max_lines or len(message) > limits.max_chars:\n",
        "    if False:\n",
        APPROVAL_TESTS,
    ),
    Mutant(
        "invisible characters in a value are shown raw",
        APPROVAL,
        "    escaped = _needs_escapes(shown, lines=False)\n",
        "    escaped = False\n",
        APPROVAL_TESTS,
    ),
    Mutant(
        "invisible characters in a text are shown raw",
        APPROVAL,
        "    escaped = _needs_escapes(text, lines=True)\n",
        "    escaped = False\n",
        APPROVAL_TESTS,
    ),
    Mutant(
        "trailing spaces are invisible",
        APPROVAL,
        '        if line != line.rstrip(" "):\n            return True\n',
        "",
        APPROVAL_TESTS,
    ),
    Mutant(
        "a leading combining mark is shown raw",
        APPROVAL,
        '    return first and category[0] == "M"\n',
        "    return False\n",
        APPROVAL_TESTS,
    ),
    Mutant(
        "arguments are shown as one value",
        APPROVAL,
        '        lines += _value_lines(f"  [{number}]", argument, width)\n',
        '        lines += _value_lines(f"  [{number}]", "\\n".join(arguments[number - 1 :]), width)\n',
        APPROVAL_TESTS,
    ),
    Mutant(
        "text lines have no gutter",
        APPROVAL,
        "        lines.append(_GUTTER + chunks[0])\n",
        "        lines.append(chunks[0])\n",
        APPROVAL_TESTS,
    ),
    Mutant(
        "wide characters count as one cell",
        APPROVAL,
        '    return 2 if unicodedata.east_asian_width(token) in ("W", "F") else 1\n',
        "    return 1\n",
        APPROVAL_TESTS,
    ),
    Mutant(
        "the desktop dialog uses the client's limits",
        APPROVAL,
        "        return self._limits if self._limits is not None else desktop_dialog_limits()\n",
        "        return CLIENT_DIALOG_LIMITS\n",
        APPROVAL_TESTS,
    ),
    Mutant(
        "an oversized request goes to the dialog",
        GATE,
        "                return _Outcome(ApprovalDecision.TOO_LARGE, channel.name, canonical, str(exc))\n",
        "                message = str(exc)\n",
        APPROVAL_TESTS,
    ),
    Mutant(
        "the caller's arguments run instead of the approved ones",
        GATE,
        "        arguments_to_run = json.loads(canonical)\n",
        "        arguments_to_run = dict(arguments)\n",
        APPROVAL_TESTS,
    ),
    # The server never changes its own code.
    Mutant(
        "protected locations are never checked",
        WORKSPACE,
        "        if entry is not None:\n            raise ProtectedPathError(\n",
        "        if False:\n            raise ProtectedPathError(\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        "protected locations are checked only lexically",
        WORKSPACE,
        "        if operation in _CHANGING:\n            self._require_unprotected(real, relative)\n",
        "",
        SELF_MODIFICATION_TESTS,
        # The 8.3 short-name test reaches the real-path layer; it needs Windows.
        platforms=("win32",),
    ),
    Mutant(
        "a new file in protected code is checked by neither layer",
        WORKSPACE,
        "        self._require_unprotected(self._lexical(parts), relative)\n\n        name = parts[-1]\n",
        "\n        name = parts[-1]\n",
        SELF_MODIFICATION_TESTS,
        also=(
            (
                "        if operation in _CHANGING:\n            self._require_unprotected(real, relative)\n",
                "",
            ),
        ),
    ),
    Mutant(
        "a directory holding protected code can be moved",
        WORKSPACE,
        "            entry = self._trusted.contained_in(path)\n",
        "            pass\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        "a workspace without explicit protection protects nothing",
        WORKSPACE,
        "        self._trusted = trusted if trusted is not None else runtime_trusted_paths()\n",
        "        self._trusted = trusted if trusted is not None else TrustedPaths()\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        "the import path is not protected",
        TRUSTED,
        "            path = tuple(entry for entry in list(sys.path) if isinstance(entry, str))\n",
        "            path = ()\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        "loaded modules are not protected",
        TRUSTED,
        "            for location in _module_locations(module):\n",
        "            for location in []:\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        "every protected location exempts the workspace below it",
        TRUSTED,
        "            if entry.above_exempt and entry.path != root and _is_within(root, entry.path):\n",
        "            if entry.path != root and _is_within(root, entry.path):\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        "the workspace on the import path is writable",
        TRUSTED,
        "            if entry.above_exempt and entry.path != root and _is_within(root, entry.path):\n",
        "            if entry.above_exempt and _is_within(root, entry.path):\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        "an import path above the workspace protects all of it",
        TRUSTED,
        "            if entry.above_exempt and entry.path != root and _is_within(root, entry.path):\n",
        "            if False:\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        "clients start the server without -I",
        CLIENTS,
        '        "args": ["-I", "-m", "universal_ai_tools.server"],\n',
        '        "args": ["-m", "universal_ai_tools.server"],\n',
        (*CLIENT_TESTS, *SELF_MODIFICATION_TESTS),
    ),
    # The audit log records every call, and no secrets.
    Mutant(
        "read-only calls are not audited",
        GATE,
        '        return self._audit.try_record("finished", finished)\n',
        '        if record.get("risk") == "read_only":\n            return True\n'
        '        return self._audit.try_record("finished", finished)\n',
        AUDIT_TESTS,
    ),
    Mutant(
        "calls refused before they start are not audited",
        GATE,
        '            record["target"] = dict(description.audit) if description is not None else {}\n'
        "        except ToolCallError as exc:\n"
        "            self._refused(record, exc)\n",
        '            record["target"] = dict(description.audit) if description is not None else {}\n'
        "        except ToolCallError as exc:\n",
        AUDIT_TESTS,
    ),
    Mutant(
        "failures are not classified",
        GATE,
        '            finished["error_class"] = error_class(exc)\n',
        "",
        AUDIT_TESTS,
    ),
    Mutant(
        "reads record no target",
        TOOLS,
        "            describe=lambda a: target(a.path),\n",
        "",
        AUDIT_TESTS,
    ),
    Mutant(
        "search patterns reach the audit log",
        TOOLS,
        "                a.path, pattern_chars=len(a.pattern), case_sensitive=a.case_sensitive\n",
        "                a.path, pattern=a.pattern, case_sensitive=a.case_sensitive\n",
        AUDIT_TESTS,
    ),
    Mutant(
        "credentials in command arguments reach the audit log",
        TERMINAL,
        '                "args": redacted_arguments(list(arguments.args)),\n',
        '                "args": list(arguments.args),\n',
        AUDIT_TESTS,
    ),
    # Stage 1: no general command execution, and refusal whenever a safeguard fails.
    Mutant(
        "the config file accepts the terminal",
        SETTINGS,
        "    if terminal.enabled:\n",
        "    if False:\n",
        (*FAIL_CLOSED_TESTS, *SETTINGS_TESTS),
    ),
    Mutant(
        "the registry registers run_command again",
        TOOLS,
        "        # run_command is never registered, whatever the configuration says: a started program\n"
        "        # cannot be confined (settings.TERMINAL_DISABLED). The terminal module is not imported.\n",
        "        if service.terminal.enabled:\n"
        "            from universal_ai_tools import terminal\n\n"
        "            specs += terminal.specs(workspace, service.terminal)\n",
        ("tests/test_file_mode_server.py", "tests/test_no_subprocess.py"),
    ),
    Mutant(
        "setup can enable the terminal",
        CLI,
        "    if enable & set(DISABLE_ONLY):\n",
        "    if False:\n",
        FAIL_CLOSED_TESTS,
    ),
    Mutant(
        "a code-executing tool gets a session grant",
        GATE,
        "        if spec.risk is ToolRisk.EXECUTE or spec.always_ask:\n",
        "        if spec.always_ask:\n",
        FAIL_CLOSED_TESTS,
    ),
    Mutant(
        "a result is returned though its record failed",
        GATE,
        '        if self._finished(record, started, "succeeded"):\n',
        '        if self._finished(record, started, "succeeded") or True:\n',
        (*FAIL_CLOSED_TESTS, "tests/test_gate.py"),
    ),
    Mutant(
        "an approval question is asked though its record failed",
        GATE,
        "                if not self._audit.try_record(\n",
        "                if False and not self._audit.try_record(\n",
        FAIL_CLOSED_TESTS,
    ),
    Mutant(
        "the server starts without an audit log",
        SERVER,
        "        except AuditError as exc:\n"
        "            # Fail closed: a server whose actions cannot be recorded does not start.\n",
        "        except AuditError as exc:\n            pass\n        if False:\n",
        FAIL_CLOSED_TESTS,
    ),
    Mutant(
        "the server starts where it imports from the current directory",
        SERVER,
        "    if current_directory_on_import_path():\n",
        "    if False:\n",
        (*FAIL_CLOSED_TESTS, "tests/test_server.py"),
    ),
    Mutant(
        "environment mode can write",
        SETTINGS,
        "        if app.enable_write_tools:\n            raise ConfigError(ENVIRONMENT_WRITES_REFUSED)\n",
        "",
        (*SETTINGS_TESTS, "tests/test_server.py"),
    ),
    # Stage 1: the protected runtime follows the process.
    Mutant(
        "late imports and import paths are not followed",
        TRUSTED,
        "        return self._entries + self._runtime.refresh()\n",
        "        return self._entries + self._runtime._entries\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        ".pth files are not read",
        TRUSTED,
        "        if not directory or directory in self._scanned:\n            return\n",
        "        return\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        "editable finders are not followed",
        TRUSTED,
        "                for location in _editable_locations(name, module):\n",
        "                for location in []:\n",
        SELF_MODIFICATION_TESTS,
    ),
    Mutant(
        "autostart folders are writable",
        TRUSTED,
        "        for path in autostart_paths():\n            _add(found, path, AUTOSTART)\n",
        "",
        SELF_MODIFICATION_TESTS,
        platforms=("win32",),
    ),
    # Stage 3: the Claude Code guard.
    Mutant(
        "guard: the protected set is not checked",
        GUARD,
        "        if entry is not None:\n            return _deny(f\"'{raw}' is protected",
        "        if False:\n            return _deny(f\"'{raw}' is protected",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: repository metadata is writable",
        GUARD,
        "            if part in METADATA_NAMES:\n",
        "            if False:\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: credential directories are writable",
        GUARD,
        "            if part in SENSITIVE_DIRECTORY_NAMES:\n                return _deny(",
        "            if False:\n                return _deny(",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: a project's Claude settings are writable",
        GUARD,
        "                and parts[index + 1] in CLAUDE_PROJECT_DENIED\n",
        "                and False\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: hard links are not detected",
        GUARD,
        "    return stat.S_ISREG(st.st_mode) and st.st_nlink > 1\n",
        "    return False\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: all of ~/.claude counts as memory",
        GUARD,
        '    return len(lowered) >= 1 and lowered[0] == "plans"\n',
        "    return True\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: memory needs no memory folder",
        GUARD,
        '    if len(lowered) >= 3 and lowered[0] == "projects" and lowered[2] == "memory":\n',
        '    if len(lowered) >= 1 and lowered[0] == "projects":\n',
        GUARD_TESTS,
    ),
    Mutant(
        "guard: home shell startup files are writable",
        GUARD,
        "for name in HOME_SHELL_FILES))",
        "for name in ()))",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: alternate data streams are allowed",
        GUARD,
        '        if ":" in real[2:]:\n',
        "        if False:\n",
        GUARD_TESTS,
        platforms=("win32",),
    ),
    Mutant(
        "guard: network and device paths are allowed",
        GUARD,
        '        if real.startswith("\\\\\\\\"):\n',
        "        if False:\n",
        GUARD_TESTS,
        platforms=("win32",),
    ),
    Mutant(
        "guard: names ending in a dot or space are allowed",
        GUARD,
        '        if not reading and any(part != part.rstrip(". ") for part in parts):\n',
        "        if False:\n",
        GUARD_TESTS,
        platforms=("win32",),
    ),
    Mutant(
        "guard: commands are not asked",
        GUARD,
        '        if self.settings.commands == "ask":\n',
        "        if False:\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: command markers are ignored",
        GUARD,
        "        named = [marker for marker in COMMAND_MARKERS if marker in folded]\n",
        "        named = []\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: Claude Code's login token is readable",
        GUARD,
        "    if path == credentials:\n",
        "    if False:\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: secrets are read without asking",
        GUARD,
        "    if parts and is_sensitive_file_name(parts[-1]):\n",
        "    if False:\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: an unrecordable change runs",
        GUARD,
        '            if kind in ("change", "command") and verdict.decision != "deny":\n',
        "            if False:\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: without a configuration everything runs",
        GUARD,
        '        if kind == "read":\n            home = Path.home()\n',
        "        if True:\n            home = Path.home()\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: an unexpected error lets the call through",
        GUARD,
        '        code, out, err = 2, "", PREFIX + f"failed unexpectedly',
        '        code, out, err = 0, "", PREFIX + f"failed unexpectedly',
        GUARD_TESTS,
    ),
    Mutant(
        "guard: a malformed event lets the call through",
        GUARD,
        '        return 2, "", PREFIX + "the hook event is not valid JSON; refusing."',
        '        return 0, "", PREFIX + "the hook event is not valid JSON; refusing."',
        GUARD_TESTS,
    ),
    Mutant(
        "guard: a refusal exits 0",
        GUARD,
        '        return 2, "", verdict.reason\n',
        '        return 0, "", verdict.reason\n',
        GUARD_TESTS,
    ),
    Mutant(
        "guard: ask becomes allow",
        GUARD,
        '                "permissionDecision": "ask",\n',
        '                "permissionDecision": "allow",\n',
        GUARD_TESTS,
    ),
    Mutant(
        "guard: an unknown file tool is not checked",
        GUARD,
        '    if not tool.startswith("mcp__") and ("Edit" in tool or "Write" in tool):\n',
        "    if False:\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: the weakest answer wins",
        GUARD,
        "        if order[verdict.decision] > order[best.decision]:\n",
        "        if order[verdict.decision] < order[best.decision]:\n",
        GUARD_TESTS,
    ),
    Mutant(
        "guard: --token values are logged",
        GUARD,
        "    text = _SECRET_FLAG.sub(lambda m: m.group(1) + REDACTED, text)\n",
        "",
        GUARD_TESTS,
    ),
    Mutant(
        "guard hook: started without -I",
        CLIENTS,
        '        "args": ["-I", "-m", GUARD_MODULE, "--config", str(config_path)],\n',
        '        "args": ["-m", GUARD_MODULE, "--config", str(config_path)],\n',
        GUARD_SETUP_TESTS,
    ),
    Mutant(
        "guard hook: fails open",
        CLIENTS,
        '[{**handler, "onFailure": "block"}]',
        "[handler]",
        GUARD_SETUP_TESTS,
    ),
    Mutant(
        "guard hook: a settings file changed meanwhile is overwritten",
        CLIENTS,
        '    if current != plan.before:\n        raise ClientConfigError(f"{plan.path} changed since the plan was made; run it again")',
        '    if False:\n        raise ClientConfigError(f"{plan.path} changed since the plan was made; run it again")',
        GUARD_SETUP_TESTS,
    ),
    Mutant(
        "guard hook: other hooks are dropped",
        CLIENTS,
        '            if others:\n                kept.append({**group, "hooks": others})\n            elif',
        '            if False:\n                kept.append({**group, "hooks": others})\n            elif',
        GUARD_SETUP_TESTS,
    ),
    # The cloud review of 2026-11-10.
    Mutant(
        "review: the temporary name comes from the file's name",
        WORKSPACE,
        '            temporary = f".uat-edit-{secrets.token_hex(8)}.tmp"\n',
        '            temporary = f".{name}.uat-{secrets.token_hex(8)}.tmp"\n',
        REVIEW_TESTS,
    ),
    Mutant(
        "review: an edit drops the file's permissions",
        WORKSPACE,
        "                _copy_mode(parent, temporary, stat.S_IMODE(st.st_mode))\n",
        "",
        REVIEW_TESTS,
        platforms=("linux",),
    ),
    Mutant(
        "review: an in-place change during an edit is overwritten",
        WORKSPACE,
        "                    or current.st_size != st.st_size\n                    or current.st_mtime_ns != st.st_mtime_ns\n",
        "",
        REVIEW_TESTS,
    ),
    Mutant(
        "review: a directory's contents are not checked before a move",
        WORKSPACE,
        "                if is_directory:\n                    _refuse_contents(real_source, source_parts, source)\n",
        "",
        REVIEW_TESTS,
    ),
    Mutant(
        "review: a huge directory is moved unchecked",
        WORKSPACE,
        "            if checked > MAX_MOVE_CHECKED_ENTRIES:\n",
        "            if False:\n",
        REVIEW_TESTS,
    ),
    Mutant(
        "review: a case-only rename is refused",
        WORKSPACE,
        "                        same_as=st if _case_only(source_parts, destination_parts) else None,\n",
        "                        same_as=None,\n",
        REVIEW_TESTS,
    ),
    Mutant(
        "review: the same path counts as inside itself",
        WORKSPACE,
        "    if len(candidate) <= len(container):\n        return False\n",
        "    if len(candidate) < len(container):\n        return False\n",
        REVIEW_TESTS,
    ),
    # Step 4: the Docker sandbox.
    Mutant(
        "sandbox: the network is on",
        SANDBOX,
        '        "--network", "bridge" if limits.network else "none",\n',
        '        "--network", "bridge",\n',
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: capabilities are kept",
        SANDBOX,
        '        "--cap-drop", "ALL",\n',
        "",
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: privilege escalation is allowed",
        SANDBOX,
        '        "--security-opt", "no-new-privileges",\n',
        "",
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: the workspace is writable",
        SANDBOX,
        '    if not limits.writable:\n        mount += ",readonly"\n',
        "",
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: images are pulled during a run",
        SANDBOX,
        '        "--pull", "never",\n',
        "",
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: the container runs as root",
        SANDBOX,
        '        "--user", CONTAINER_USER,\n',
        "",
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: no process limit",
        SANDBOX,
        '        "--pids-limit", str(PIDS_LIMIT),\n',
        "",
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: a changed docker program runs",
        SANDBOX,
        "    if fingerprint(docker) != expected_sha256.lower():\n",
        "    if False:\n",
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: a slow command is not killed",
        SANDBOX,
        "        kill(docker, container)\n",
        "",
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: server settings reach docker",
        SANDBOX,
        "    return {k: v for k, v in os.environ.items() if not k.startswith(ENV_PREFIX)}\n",
        "    return dict(os.environ)\n",
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: secrets in commands are audited",
        SANDBOX,
        '                "command": redact(a.command),\n',
        '                "command": a.command,\n',
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: output control characters are passed on",
        SANDBOX,
        "            stdout=escape_text(done.stdout)[0],\n",
        "            stdout=done.stdout,\n",
        SANDBOX_TESTS,
    ),
    Mutant(
        "sandbox: output is not capped",
        SANDBOX,
        '    return data[:limit].decode("utf-8", "replace"), len(data) > limit\n',
        '    return data.decode("utf-8", "replace"), False\n',
        SANDBOX_TESTS,
    ),
    # Saving screenshots.
    Mutant(
        "screenshots: a screen approval also allows saving files",
        DESKTOP,
        '        capability="screenshot-files",\n',
        '        capability="screen",\n',
        ("tests/test_save_screenshot.py",),
    ),
    Mutant(
        "screenshots: the gate gives saving no policy of its own",
        GATE,
        '            "screenshot-files": self._service.screen.approval,\n',
        "",
        ("tests/test_save_screenshot.py",),
    ),
    # Step 3: launching, reading, waiting.
    Mutant(
        "launch: dangerous programs are launched",
        LAUNCH,
        "    if name in REFUSED_PROGRAMS:\n",
        "    if False:\n",
        READING_TESTS,
    ),
    Mutant(
        "launch: any name is accepted",
        LAUNCH,
        "    if not _NAME.fullmatch(name):\n",
        "    if False:\n",
        READING_TESTS,
    ),
    Mutant(
        "launch: programs outside trusted folders are launched",
        LAUNCH,
        " and candidate.name.casefold() == name and _within(candidate, roots):\n",
        " and candidate.name.casefold() == name:\n",
        READING_TESTS,
    ),
    Mutant(
        "launch: an App Paths entry may name another program",
        LAUNCH,
        "candidate.is_file() and candidate.name.casefold() == name and _within(",
        "candidate.is_file() and _within(",
        READING_TESTS,
    ),
    Mutant(
        "launch: unlisted programs are launched",
        DESKTOP,
        "        if program not in listed:\n",
        "        if False:\n",
        READING_TESTS,
    ),
    Mutant(
        "launch: a stop does not stop a launch",
        DESKTOP,
        "        if self.stop.reason() is not None:\n            raise InputRefused(self.stop.message())\n        try:\n            path = launch.resolve",
        "        try:\n            path = launch.resolve",
        READING_TESTS,
    ),
    Mutant(
        "launch: a launch can be covered by a session approval",
        DESKTOP,
        '        operation="launch",\n        always_ask=True,\n',
        '        operation="launch",\n        always_ask=False,\n',
        READING_TESTS,
    ),
    Mutant(
        "reading: a password field reported late is read",
        DESKTOP,
        '        if live.password:\n            raise ToolCallError("password fields are never read")\n',
        "",
        READING_TESTS,
    ),
    Mutant(
        "reading: an element of another program is read",
        DESKTOP,
        "        if live is None or live.process_id != window.pid:\n",
        "        if live is None:\n",
        READING_TESTS,
    ),
    Mutant(
        "reading: a replaced window is read",
        DESKTOP,
        "        if window is None or window.pid != inspection.pid:\n",
        "        if window is None:\n",
        READING_TESTS,
    ),
    Mutant(
        "reading: control characters in text are passed on",
        DESKTOP,
        "        safe, _escaped = escape_text(text)\n",
        "        safe = text\n",
        READING_TESTS,
    ),
    Mutant(
        "reading: control characters in tables are passed on",
        DESKTOP,
        "            rows=[[escape_line(cell)[:500] for cell in row] for row in data.rows],\n",
        "            rows=[list(row) for row in data.rows],\n",
        READING_TESTS,
    ),
    Mutant(
        "inspect: the control type filter is ignored",
        DESKTOP,
        "            and (not kind or kind == element.control_type.casefold())\n",
        "            and True\n",
        READING_TESTS,
    ),
    # Step 2: input grants, the banner, and the stop hotkey.
    Mutant(
        "grants: the grant request can be covered by a session approval",
        GATE,
        "        if spec.risk is ToolRisk.EXECUTE or spec.always_ask:\n",
        "        if spec.risk is ToolRisk.EXECUTE:\n",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: grants never expire",
        GRANTS,
        "                if grant.expires > now and grant.issued > revoked_at\n",
        "                if grant.issued > revoked_at\n",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: revocation is ignored",
        GRANTS,
        "                if grant.expires > now and grant.issued > revoked_at\n",
        "                if grant.expires > now\n",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: an unreadable revocation counts as none",
        GRANTS,
        '        except ValueError:\n            return float("inf")\n',
        '        except ValueError:\n            return float("-inf")\n',
        GRANT_TESTS,
    ),
    Mutant(
        "grants: any tool is covered",
        GRANTS,
        "        if tool not in GRANT_TOOLS or not program:\n",
        "        if not program:\n",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: any program is covered",
        GRANTS,
        "            if program in grant.programs:\n",
        "            if True:\n",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: protected windows are covered",
        DESKTOP,
        "        if window is None or self.refusal(window) is not None:\n            return None\n        if self.stop",
        "        if window is None:\n            return None\n        if self.stop",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: a stop does not suspend grants",
        DESKTOP,
        "        if self.stop.reason() is not None:\n            return None\n        grant = self.grants.covering",
        "        grant = self.grants.covering",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: programs outside the allowed list are granted",
        DESKTOP,
        "        outside = [p for p in programs if p not in allowed or p in PROTECTED_PROCESSES]\n",
        "        outside = [p for p in programs if p in PROTECTED_PROCESSES]\n",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: protected programs are granted",
        DESKTOP,
        "        outside = [p for p in programs if p not in allowed or p in PROTECTED_PROCESSES]\n",
        "        outside = [p for p in programs if p not in allowed]\n",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: the time limit is ignored",
        DESKTOP,
        "        if arguments.minutes > settings.max_grant_minutes:\n",
        "        if False:\n",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: a halt leaves grants running",
        DESKTOP,
        "        self.stop.trigger(reason)\n        self.grants.revoke_all()\n",
        "        self.stop.trigger(reason)\n",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: the banner shows without a grant",
        DESKTOP,
        "        if not grants or self.stop.reason() is not None:\n",
        "        if self.stop.reason() is not None:\n",
        GRANT_TESTS,
    ),
    Mutant(
        "grants: input stop from the CLI leaves grants running",
        CLI,
        "        # Stopping ends every grant; resuming does not bring one back.\n        revoke_all(revoked_file(path.parent))\n",
        "        # Stopping ends every grant; resuming does not bring one back.\n        pass\n",
        GRANT_TESTS,
    ),
    # Stage 4: the emergency stop and controls by name.
    Mutant(
        "stop: a latched stop is ignored",
        DESKTOP,
        '        """Refuse if input is stopped, or stop it now if the pointer is in a corner."""\n'
        "        if self.stop.reason() is not None:\n            raise InputRefused(self.stop.message())\n",
        '        """Refuse if input is stopped, or stop it now if the pointer is in a corner."""\n',
        CONTROL_TESTS,
    ),
    Mutant(
        "stop: the corner is not checked before input",
        DESKTOP,
        "        x, y = self._backend.cursor_position()\n        if in_corner(x, y, self._backend.primary_screen()):\n",
        "        x, y = self._backend.cursor_position()\n        if False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "stop: targets skip the stop check",
        DESKTOP,
        "        self.check_stop()\n",
        "",
        CONTROL_TESTS,
    ),
    Mutant(
        "stop: clicks into a corner are allowed",
        DESKTOP,
        "    def _refuse_corner(self, x: int, y: int) -> None:\n        if in_corner(",
        "    def _refuse_corner(self, x: int, y: int) -> None:\n        if False and in_corner(",
        CONTROL_TESTS,
    ),
    Mutant(
        "stop: typing ignores the user's mouse",
        DESKTOP,
        "                elif max(abs(x - start[0]), abs(y - start[1])) > POINTER_TOLERANCE:\n",
        "                elif False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "stop: typing ignores the corner",
        DESKTOP,
        "                if in_corner(x, y, self._backend.primary_screen()):\n",
        "                if False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "stop: edges count as corners",
        STOP,
        "    return (near_left or near_right) and (near_top or near_bottom)\n",
        "    return (near_left or near_right) or (near_top or near_bottom)\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "stop: the stop is not shared between processes",
        STOP,
        '                self._path.write_text(json.dumps(record) + "\\n", encoding="utf-8")\n                return\n',
        "",
        CONTROL_TESTS,
    ),
    Mutant(
        "controls: a password field's text is shown",
        DESKTOP,
        "                    if element.password or element.value is None\n",
        "                    if element.value is None\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "controls: a password field can be set",
        DESKTOP,
        '            if action == "set_value" and live.password:\n',
        "            if False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "controls: an element of another program is used",
        DESKTOP,
        "            if live.process_id != window.pid:\n",
        "            if False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "controls: a disabled element is used",
        DESKTOP,
        "            if not live.enabled:\n",
        "            if False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "controls: unsupported actions are attempted",
        DESKTOP,
        "            if action not in live.actions:\n",
        "            if False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "controls: a vanished element is used",
        DESKTOP,
        "            if live is None:\n                raise InputRefused(",
        "            if False:\n                raise InputRefused(",
        CONTROL_TESTS,
    ),
    Mutant(
        "controls: an inspection is used for another window",
        DESKTOP,
        "        if inspection.window_id != window_id:\n",
        "        if False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "controls: inspections never expire",
        DESKTOP,
        "        if self._clock() - inspection.taken > INSPECTION_TTL_SECONDS:\n",
        "        if False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "controls: a replaced window is used",
        DESKTOP,
        "            if window.pid != pid:\n",
        "            if False:\n",
        CONTROL_TESTS,
    ),
    Mutant(
        "controls: the new text is audited",
        DESKTOP,
        '                    "characters": len(a.value),\n',
        '                    "value": a.value,\n',
        CONTROL_TESTS,
    ),
]


def classify_failure(report: Path) -> tuple[str, str]:
    """Return how the first failing test failed, and its name, from a JUnit report."""
    for case in ElementTree.parse(report).getroot().iter("testcase"):  # noqa: S314
        for problem in (*case.findall("failure"), *case.findall("error")):
            name = f"{case.get('classname')}::{case.get('name')}"
            message = problem.get("message") or ""
            if "DID NOT RAISE" in message or "Regex pattern did not match" in message:
                return KILLED_BY_EXPECTED_EXCEPTION, name
            if message.startswith(("assert ", "AssertionError", "Failed:")):
                return KILLED_BY_ASSERTION, name
            return KILLED_BY_UNEXPECTED_EXCEPTION, name
    return COULD_NOT_TEST, "pytest failed without a failing test"


def run_mutant(mutant: Mutant, scratch: Path, can_symlink: bool) -> tuple[Mutant, str, str]:
    """Apply one mutant to its own copy and return its outcome and the evidence for it."""
    if mutant.needs_symlinks and not can_symlink:
        return mutant, COULD_NOT_TEST, "needs symlink privilege"
    copy = Path(tempfile.mkdtemp(prefix="mutant", dir=scratch))
    shutil.copytree(REPOSITORY / "src", copy / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(
        REPOSITORY / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__")
    )
    for name in ("pyproject.toml", ".env.example"):
        shutil.copy(REPOSITORY / name, copy)
    target = copy / "src" / "universal_ai_tools" / mutant.file
    source = target.read_text(encoding="utf-8")
    for old, new in ((mutant.old, mutant.new), *mutant.also):
        if source.count(old) != 1:
            return mutant, COULD_NOT_TEST, f"stale: {source.count(old)} matches"
        source = source.replace(old, new)
    target.write_text(source, encoding="utf-8", newline="\n")
    env = os.environ | {"PYTHONPATH": str(copy / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    report = copy / "report.xml"
    try:
        done = run_tests(mutant, copy, env, report)
        if done.returncode == 0:
            return mutant, SURVIVED, ""
        if done.returncode == 1:
            return (mutant, *classify_failure(report))
        return mutant, COULD_NOT_TEST, f"pytest exit {done.returncode}"
    except subprocess.TimeoutExpired:
        return mutant, TIMED_OUT, f"no result in {TIMEOUT_SECONDS} s"
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def run_tests(
    mutant: Mutant, copy: Path, env: dict[str, str], report: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-x",
            "-q",
            "--no-header",
            "--tb=no",
            "-p",
            "no:cacheprovider",
            f"--junit-xml={report}",
            *mutant.tests,
        ],
        cwd=copy,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def can_create_symlinks(scratch: Path) -> bool:
    try:
        (scratch / "symlink-probe").symlink_to(scratch)
    except OSError:
        return False
    (scratch / "symlink-probe").unlink()
    return True


def main() -> int:
    """Run every mutant, or with arguments only those of the named source files."""
    files = set(sys.argv[1:])
    unknown = files - {mutant.file for mutant in MUTANTS}
    if unknown:
        print(f"no mutants for: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    selected = [
        mutant
        for mutant in MUTANTS
        if sys.platform in mutant.platforms and (not files or mutant.file in files)
    ]
    workers = max(2, (os.cpu_count() or 2) // 2)
    with (
        tempfile.TemporaryDirectory(prefix="mutation") as scratch,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        can_symlink = can_create_symlinks(Path(scratch))
        outcomes = list(
            pool.map(lambda mutant: run_mutant(mutant, Path(scratch), can_symlink), selected)
        )
    for mutant, outcome, evidence in outcomes:
        print(f"{outcome} | {mutant.name} [{mutant.file}] | {evidence}")
    print()
    print(f"platform: {sys.platform}, symlink privilege: {can_symlink}")
    totals = Counter(outcome for _, outcome, _ in outcomes)
    for outcome in OUTCOMES:
        print(f"{totals[outcome]:4} {outcome}")
    print(f"{len(MUTANTS) - len(selected):4} not applicable on this platform")
    unresolved = totals[SURVIVED] + totals[TIMED_OUT] + totals[COULD_NOT_TEST]
    print(f"{len(outcomes)} run, {unresolved} unresolved")
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())

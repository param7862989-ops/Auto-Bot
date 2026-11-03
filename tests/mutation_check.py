"""Mutation check for the filesystem and process safeguards.

Run it directly: ``python tests/mutation_check.py``. It removes one safeguard at a time
from a scratch copy of the source and requires the tests to fail, which shows a test
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
RACE_TESTS = ("tests/test_races.py::test_concurrent_parent_replacement_never_escapes",)


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
        "if operation is Operation.CREATE and any(",
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
        "        _enforce(parts, Operation.CREATE, relative)\n",
        "",
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
        "                self._require_allowed(real, relative, Operation.READ)\n",
        "",
    ),
    Mutant(
        "real path may leave the root",
        WORKSPACE,
        "            inside = Path(real).relative_to(self._root).parts\n"
        "        except ValueError:\n"
        '            raise WorkspaceError(f"path resolves outside the workspace: '
        "'{relative}'\") from None\n",
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
        '                raise WorkspaceError(f"refusing to follow symlink or junction: '
        "'{prefix}'\")\n",
        "            st = self.lstat(name)\n",
        platforms=("linux",),
    ),
    Mutant(
        "directory link is not inspected (Windows)",
        WORKSPACE,
        "                if _is_redirect(info.attributes, info.reparse_tag):\n"
        '                    raise WorkspaceError(f"refusing to follow symlink or junction: '
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
                "                self._require_allowed(real, relative, Operation.READ)\n",
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
        '        return WorkspaceError(f"refusing to follow symlink or junction: '
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
        '        raise WorkspaceError("path is not valid text for this filesystem") from None\n',
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
        "    if removed:\n",
        "    if False:\n",
        ("tests/test_config.py", "tests/test_server.py"),
    ),
    Mutant(
        "a value of false is accepted",
        CONFIG,
        'removed = [name for name in REMOVED_SETTINGS if source.get(name, "").strip()]',
        "removed = [name for name in REMOVED_SETTINGS "
        'if source.get(name, "").strip() not in ("", "false")]',
        ("tests/test_config.py",),
    ),
    Mutant(
        "a tool call starts a process",
        SERVER,
        "    return PlatformInfo(name=APP_NAME,",
        '    __import__("subprocess").run([sys.executable, "-c", "pass"], check=True)\n'
        "    return PlatformInfo(name=APP_NAME,",
        ("tests/test_no_subprocess.py",),
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
    selected = [mutant for mutant in MUTANTS if sys.platform in mutant.platforms]
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

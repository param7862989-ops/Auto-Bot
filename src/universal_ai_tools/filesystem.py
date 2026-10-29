"""Filesystem tools: read-only ``list_files``, ``read_file``, and ``search_files``, and
create-only ``write_file``.

Plain functions with no MCP imports. Every path goes through ``Workspace``, which
confines access to the authorized root and refuses links. Failures are raised as
``WorkspaceError`` with client-safe messages. ``write_file`` creates new files only;
nothing here overwrites, appends to, renames, or deletes files, runs commands, or
reads environment variables.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from universal_ai_tools.workspace import (
    SENSITIVE_DIRECTORY_NAMES,
    FileTooLargeError,
    ResolvedPath,
    Workspace,
    WorkspaceError,
    is_sensitive_file_name,
)

# Generated, vendored, or VCS directories that recursive listing and search do not
# descend into. Matched case-insensitively by name, at any depth below the start path.
# write_file refuses to create anything inside them, or with one of these names.
EXCLUDED_DIRECTORY_NAMES = SENSITIVE_DIRECTORY_NAMES | frozenset(
    {
        ".git",
        ".hg",
        ".svn",
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
_EXCLUDED_DIRECTORY_SUFFIXES = (".egg-info",)

# Upper bounds advertised in the tool input schemas. FilesystemLimits may lower them.
MAX_LIST_ENTRIES = 1000
DEFAULT_LIST_ENTRIES = 200
MAX_SEARCH_RESULTS = 500
DEFAULT_SEARCH_RESULTS = 100
MAX_PATTERN_LENGTH = 200

EntryType = Literal["file", "directory", "symlink", "other"]


@dataclass(frozen=True)
class FilesystemLimits:
    """Resource limits for the filesystem tools."""

    max_list_entries: int = MAX_LIST_ENTRIES
    max_read_bytes: int = 1024 * 1024
    max_pattern_length: int = MAX_PATTERN_LENGTH
    max_search_results: int = MAX_SEARCH_RESULTS
    max_files_scanned: int = 5000
    max_search_file_bytes: int = 1024 * 1024
    search_timeout_seconds: float = 5.0
    max_snippet_chars: int = 200
    max_write_bytes: int = 1024 * 1024


DEFAULT_LIMITS = FilesystemLimits()


def is_excluded_directory_name(name: str) -> bool:
    folded = name.casefold()
    return folded in EXCLUDED_DIRECTORY_NAMES or folded.endswith(_EXCLUDED_DIRECTORY_SUFFIXES)


# Result models


class FileEntry(BaseModel):
    path: str = Field(description="Path relative to the workspace root, with '/' separators.")
    type: EntryType = Field(
        description="'symlink' covers symlinks and Windows junctions; they are never followed."
    )
    size_bytes: int | None = Field(description="Size for regular files; null otherwise.")


class ListFilesResult(BaseModel):
    path: str = Field(description="The listed directory, relative to the workspace root.")
    entries: list[FileEntry] = Field(description="Entries in sorted, depth-first order.")
    truncated: bool = Field(description="True if more entries exist than max_entries allowed.")
    skipped_directories: int = Field(
        description="Directories not descended into during a recursive listing because they "
        "are excluded (e.g. .git, .venv, caches) or unreadable. They still appear as entries."
    )


class ReadFileResult(BaseModel):
    path: str = Field(description="The file read, relative to the workspace root.")
    size_bytes: int = Field(description="Size of the file in bytes.")
    content: str = Field(description="Full file content decoded as UTF-8.")


class SearchMatch(BaseModel):
    path: str = Field(description="File containing the match, relative to the workspace root.")
    line: int = Field(description="1-based line number.")
    column: int = Field(description="1-based column of the first match on the line.")
    snippet: str = Field(description="The matching line, shortened around the match if long.")


class SearchSkips(BaseModel):
    excluded_directories: int = Field(description="Excluded directories not searched.")
    unreadable_directories: int = Field(description="Directories that could not be listed.")
    symlinks: int = Field(description="Symlinks and junctions, which are never followed.")
    sensitive_files: int = Field(description="Credential-like files such as .env or *.pem.")
    too_large: int = Field(description="Files over the per-file size limit.")
    binary: int = Field(description="Files containing NUL bytes.")
    not_utf8: int = Field(description="Files that are not valid UTF-8.")
    unreadable_files: int = Field(description="Files that could not be opened or read.")


class SearchFilesResult(BaseModel):
    pattern: str = Field(description="The literal text searched for.")
    path: str = Field(description="The searched directory, relative to the workspace root.")
    matches: list[SearchMatch] = Field(description="Matches in sorted path, then line, order.")
    files_scanned: int = Field(
        description="Files read and checked, counting those then skipped as binary or not UTF-8."
    )
    truncated: bool = Field(description="True if the search stopped early; see truncation_reason.")
    truncation_reason: Literal["max_results", "max_files_scanned", "time_limit"] | None = Field(
        description="Why the search stopped early, or null if it completed."
    )
    skipped: SearchSkips = Field(description="Counts of what was not searched, by reason.")


class WriteFileResult(BaseModel):
    path: str = Field(description="The created file, relative to the workspace root.")
    bytes_written: int = Field(description="Size of the new file: the content's UTF-8 length.")


# Tools


def list_files(
    workspace: Workspace,
    path: str = ".",
    *,
    recursive: bool = False,
    max_entries: int = DEFAULT_LIST_ENTRIES,
    limits: FilesystemLimits = DEFAULT_LIMITS,
) -> ListFilesResult:
    """List a directory inside the workspace without following links."""
    limit = _bounded(max_entries, limits.max_list_entries, "max_entries")
    start = workspace.resolve(path)
    if not start.is_dir:
        raise WorkspaceError(f"not a directory: '{start.relative}'")

    walk = _Walk(workspace, start, recursive=recursive)
    entries: list[FileEntry] = []
    truncated = False
    for item in walk:
        if len(entries) == limit:
            truncated = True
            break
        kind = _entry_type(item)
        entries.append(
            FileEntry(
                path=item.relative,
                type=kind,
                size_bytes=item.stat.st_size if kind == "file" else None,
            )
        )
    return ListFilesResult(
        path=start.relative,
        entries=entries,
        truncated=truncated,
        skipped_directories=walk.excluded + walk.unreadable,
    )


def read_file(
    workspace: Workspace,
    path: str,
    *,
    limits: FilesystemLimits = DEFAULT_LIMITS,
) -> ReadFileResult:
    """Read a UTF-8 text file inside the workspace."""
    target = workspace.resolve(path, for_reading=True)
    if not target.parts:
        raise WorkspaceError("is a directory, not a file: '.'")
    data = workspace.read_bytes(target, limits.max_read_bytes)
    return ReadFileResult(path=target.relative, size_bytes=len(data), content=_decode(data, target))


def search_files(
    workspace: Workspace,
    pattern: str,
    path: str = ".",
    *,
    case_sensitive: bool = True,
    max_results: int = DEFAULT_SEARCH_RESULTS,
    limits: FilesystemLimits = DEFAULT_LIMITS,
    clock: Callable[[], float] = time.monotonic,
) -> SearchFilesResult:
    """Search text files under a workspace directory for a literal string."""
    if not pattern:
        raise WorkspaceError("pattern must not be empty")
    if len(pattern) > limits.max_pattern_length:
        raise WorkspaceError(
            f"pattern is too long: {len(pattern)} characters; "
            f"the limit is {limits.max_pattern_length}"
        )
    if "\n" in pattern or "\r" in pattern:
        raise WorkspaceError("pattern must be a single line")
    limit = _bounded(max_results, limits.max_search_results, "max_results")
    start = workspace.resolve(path)
    if not start.is_dir:
        raise WorkspaceError(f"not a directory: '{start.relative}'")

    # re.escape makes this a literal match; the pattern is never interpreted as a regex.
    matcher = re.compile(re.escape(pattern), 0 if case_sensitive else re.IGNORECASE)
    deadline = clock() + limits.search_timeout_seconds
    walk = _Walk(workspace, start, recursive=True)
    skips = SearchSkips(
        excluded_directories=0,
        unreadable_directories=0,
        symlinks=0,
        sensitive_files=0,
        too_large=0,
        binary=0,
        not_utf8=0,
        unreadable_files=0,
    )
    matches: list[SearchMatch] = []
    files_scanned = 0
    reason: Literal["max_results", "max_files_scanned", "time_limit"] | None = None

    for item in walk:
        if clock() >= deadline:
            reason = "time_limit"
            break
        if item.is_link:
            skips.symlinks += 1
            continue
        if not item.is_file:
            continue
        if is_sensitive_file_name(item.name):
            skips.sensitive_files += 1
            continue
        if item.stat.st_size > limits.max_search_file_bytes:
            skips.too_large += 1
            continue
        if files_scanned == limits.max_files_scanned:
            reason = "max_files_scanned"
            break
        try:
            data = workspace.read_bytes(item, limits.max_search_file_bytes)
        except FileTooLargeError:
            skips.too_large += 1
            continue
        except WorkspaceError:
            skips.unreadable_files += 1
            continue
        files_scanned += 1
        if b"\x00" in data:
            skips.binary += 1
            continue
        try:
            text = _decode_utf8(data)
        except UnicodeDecodeError:
            skips.not_utf8 += 1
            continue
        for number, line in enumerate(text.split("\n"), start=1):
            found = matcher.search(line)
            if found is None:
                continue
            if len(matches) == limit:
                reason = "max_results"
                break
            matches.append(
                SearchMatch(
                    path=item.relative,
                    line=number,
                    column=found.start() + 1,
                    snippet=_snippet(line.rstrip("\r"), found, limits.max_snippet_chars),
                )
            )
        if reason is not None:
            break

    skips.excluded_directories = walk.excluded
    skips.unreadable_directories = walk.unreadable
    return SearchFilesResult(
        pattern=pattern,
        path=start.relative,
        matches=matches,
        files_scanned=files_scanned,
        truncated=reason is not None,
        truncation_reason=reason,
        skipped=skips,
    )


def write_file(
    workspace: Workspace,
    path: str,
    content: str,
    *,
    limits: FilesystemLimits = DEFAULT_LIMITS,
) -> WriteFileResult:
    """Create a new UTF-8 text file inside the workspace. Never overwrites anything.

    The content is encoded as UTF-8 exactly as given: no BOM is added and newlines
    are not translated.
    """
    if "\x00" in content:
        raise WorkspaceError("content must not contain NUL characters")
    # Each character takes at least one UTF-8 byte, so this bounds the encoding work.
    if len(content) > limits.max_write_bytes:
        raise FileTooLargeError(
            f"content is too large: more than {limits.max_write_bytes} bytes as UTF-8; "
            f"the limit is {limits.max_write_bytes} bytes"
        )
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkspaceError(
            f"content is not valid Unicode text (unpaired surrogate at character {exc.start})"
        ) from None
    if len(data) > limits.max_write_bytes:
        raise FileTooLargeError(
            f"content is too large: {len(data)} bytes as UTF-8; "
            f"the limit is {limits.max_write_bytes} bytes"
        )
    created = workspace.create_file(path, data, is_protected_directory=is_excluded_directory_name)
    return WriteFileResult(path=created, bytes_written=len(data))


# Helpers


class _Walk:
    """Depth-first, name-sorted traversal that never follows links.

    Yields every entry below ``start`` (not ``start`` itself). When recursive, it
    descends into real subdirectories except excluded ones, and counts excluded
    and unreadable directories instead of failing. An unreadable ``start`` raises.
    """

    def __init__(self, workspace: Workspace, start: ResolvedPath, *, recursive: bool) -> None:
        self._workspace = workspace
        self._start = start
        self._recursive = recursive
        self.excluded = 0
        self.unreadable = 0

    def __iter__(self) -> Iterator[ResolvedPath]:
        stack = [iter(self._workspace.scan_directory(self._start))]
        while stack:
            item = next(stack[-1], None)
            if item is None:
                stack.pop()
                continue
            yield item
            if not self._recursive or item.is_link or not item.is_dir:
                continue
            if is_excluded_directory_name(item.name):
                self.excluded += 1
                continue
            try:
                stack.append(iter(self._workspace.scan_directory(item)))
            except WorkspaceError:
                self.unreadable += 1


def _entry_type(item: ResolvedPath) -> EntryType:
    if item.is_link:
        return "symlink"
    if item.is_dir:
        return "directory"
    if item.is_file:
        return "file"
    return "other"


def _bounded(value: int, maximum: int, name: str) -> int:
    if value < 1:
        raise WorkspaceError(f"{name} must be at least 1")
    return min(value, maximum)


def _decode(data: bytes, target: ResolvedPath) -> str:
    if b"\x00" in data:
        raise WorkspaceError(
            f"file appears to be binary and cannot be read as text: '{target.relative}'"
        )
    try:
        return _decode_utf8(data)
    except UnicodeDecodeError as exc:
        raise WorkspaceError(
            f"file is not valid UTF-8 text: '{target.relative}' "
            f"(invalid byte at offset {exc.start})"
        ) from None


def _decode_utf8(data: bytes) -> str:
    # Strict UTF-8, then drop one leading BOM. Unlike the "utf-8-sig" codec, this keeps
    # error offsets relative to the raw bytes, BOM included.
    return data.decode("utf-8").removeprefix("﻿")


def _snippet(line: str, found: re.Match[str], width: int) -> str:
    if len(line) <= width:
        return line.strip()
    left = max(0, found.start() - max(0, width - (found.end() - found.start())) // 2)
    left = min(left, len(line) - width)
    text = line[left : left + width]
    prefix = "…" if left > 0 else ""
    suffix = "…" if left + width < len(line) else ""
    return f"{prefix}{text}{suffix}"

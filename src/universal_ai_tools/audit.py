"""The audit log: one JSON object per line, appended by every server process.

Claude Desktop and Claude Code each run their own server process, and both append to the
same file, so a lock file serialises writers. ``universal_ai_tools.gate`` writes a record for
every call, read-only ones included: which process, session, call, and client request, which
tool and kind of operation, the target, a digest of the arguments, the permission and
approval decisions, what happened, and for a failure a fixed error class.

What is never recorded: file contents, search patterns (only their length), text typed
through ``type_text``, screenshots, or command output. A record carries the target (a
workspace path, a program and its arguments with credential-looking values redacted, a window
title), sizes and counts, the decisions, and a short error message.

A consequential action is recorded before it runs (``started``) and after (``finished``).
If the ``started`` record cannot be written, the action does not run: an action that cannot
be audited is refused. Read-only calls, and calls refused before they could run, are
recorded after the fact; a failure to record them is logged to stderr but does not fail
the call.

The log is rotated to ``<name>.1`` (replacing an older ``.1``) when it grows past its size
limit. It is a record for the user, not a tamper-proof one: anything running as the user,
including a command the user approved, can edit it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_FIELD_CHARS = 2000
_LOCK_TIMEOUT_SECONDS = 5.0


class AuditError(Exception):
    """The record could not be written."""


class AuditLog:
    """Appends records to ``path``. ``AuditLog(None)`` records nothing (environment mode)."""

    def __init__(self, path: Path | None, *, max_bytes: int = 10 * 1024 * 1024) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._thread_lock = threading.Lock()

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def record(self, event: str, fields: Mapping[str, object], *, durable: bool = False) -> None:
        """Append one record.

        Raises:
            AuditError: If the record could not be written.
        """
        if self._path is None:
            return
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event,
            "pid": os.getpid(),
            **{key: _bounded(value) for key, value in fields.items()},
        }
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        try:
            with self._thread_lock, self._locked():
                self._rotate_if_needed()
                with open(self._path, "ab") as log:
                    log.write(line)
                    log.flush()
                    if durable:
                        os.fsync(log.fileno())
        except OSError as exc:
            raise AuditError(f"cannot write the audit log: {type(exc).__name__}") from None

    def try_record(self, event: str, fields: Mapping[str, object]) -> bool:
        """Append one record; on failure log a warning and return False."""
        try:
            self.record(event, fields)
        except AuditError as exc:
            logger.warning("%s", exc)
            return False
        return True

    def _rotate_if_needed(self) -> None:
        if self._path is None:
            return
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size >= self._max_bytes:
            os.replace(self._path, self._path.with_name(self._path.name + ".1"))

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self._path is None:
            raise AuditError("the audit log is disabled")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(self._path.name + ".lock")
        with open(lock_path, "a+b") as lock:
            _acquire(lock.fileno())
            try:
                yield
            finally:
                _release(lock.fileno())


def _bounded(value: object) -> object:
    if isinstance(value, str):
        return value if len(value) <= MAX_FIELD_CHARS else value[: MAX_FIELD_CHARS - 1] + "…"
    if isinstance(value, Mapping):
        return {str(k): _bounded(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_bounded(v) for v in value[:100]]
    if value is None or isinstance(value, bool | int | float):
        return value
    return _bounded(str(value))


if sys.platform == "win32":
    import msvcrt

    def _acquire(fd: int) -> None:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)

    def _release(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _release(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def read_records(path: Path, *, limit: int = 50) -> list[dict[str, object]]:
    """The last ``limit`` records of a log, oldest first. Malformed lines are skipped."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, object]] = []
    for line in lines[-limit:]:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records

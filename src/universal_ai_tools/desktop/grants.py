"""Time-limited input permissions ("grants"): one approval for many input actions.

Approving every keystroke makes a long task impractical. With ``[input] grants = true``,
Claude can ask once (``request_input_grant``) for input to a few named programs, for a few
minutes, for a stated purpose. That request is always asked, never covered by a grant. While
a grant is active, input tool calls to windows of those programs run without a question; each
is still checked against every input rule at the moment it runs (protected windows,
``[input] allowed_processes``, the approval-dialog rule, the emergency stop) and recorded in
the audit log with the grant's id.

A grant ends when its time is up, when the emergency stop or the stop hotkey is used, or when
the user runs ``universal-ai-tools grants revoke`` (or ``input stop`` / ``input resume``).
Revoking writes the time to ``grants-revoked`` in the state directory; every server process
treats any grant issued before that time as void, so one command ends them all. Grants live
only in the memory of the server process that made them: they never outlive it, and they are
never shared with another client.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

REVOKED_FILE_NAME = "grants-revoked"
GRANT_TOOLS = frozenset(
    {
        "focus_window",
        "click",
        "scroll",
        "type_text",
        "press_keys",
        "invoke_element",
        "set_element_value",
    }
)
"""The tools a grant can cover. Never ``request_input_grant`` itself, and no screen tool."""


def revoked_file(state_directory: Path) -> Path:
    return state_directory / REVOKED_FILE_NAME


def revoke_all(path: Path, *, wall: Callable[[], float] = time.time) -> None:
    """Void every grant issued until now, in every server process."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{wall():.6f}\n", encoding="utf-8")


@dataclass(frozen=True)
class Grant:
    grant_id: str
    programs: frozenset[str]
    expires: float
    """On the store's monotonic clock."""
    issued: float
    """Wall-clock time (``time.time``), compared with the revocation file."""
    minutes: int
    purpose: str


class GrantStore:
    """The active grants of one server process."""

    def __init__(
        self,
        revoked: Path | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._revoked = revoked
        self._clock = clock
        self._wall = wall
        self._grants: dict[str, Grant] = {}
        self._lock = threading.Lock()

    def add(self, programs: Iterable[str], minutes: int, purpose: str) -> Grant:
        grant = Grant(
            grant_id=secrets.token_hex(4),
            programs=frozenset(programs),
            expires=self._clock() + minutes * 60,
            issued=self._wall(),
            minutes=minutes,
            purpose=purpose,
        )
        with self._lock:
            self._grants[grant.grant_id] = grant
        return grant

    def active(self) -> list[Grant]:
        """Grants not expired and not revoked, oldest first."""
        now = self._clock()
        revoked_at = self._revoked_at()
        with self._lock:
            self._grants = {
                key: grant
                for key, grant in self._grants.items()
                if grant.expires > now and grant.issued > revoked_at
            }
            return list(self._grants.values())

    def covering(self, tool: str, program: str) -> Grant | None:
        """An active grant that covers ``tool`` acting on a window of ``program``."""
        if tool not in GRANT_TOOLS or not program:
            return None
        for grant in self.active():
            if program in grant.programs:
                return grant
        return None

    def revoke_all(self) -> None:
        """End every grant, in this process and (through the file) in all others."""
        with self._lock:
            self._grants.clear()
        if self._revoked is not None:
            # This process's grants are gone even if the file cannot be written.
            with contextlib.suppress(OSError):
                revoke_all(self._revoked, wall=self._wall)

    def remaining_seconds(self) -> float:
        grants = self.active()
        if not grants:
            return 0.0
        return max(grant.expires for grant in grants) - self._clock()

    def _revoked_at(self) -> float:
        if self._revoked is None:
            return float("-inf")
        try:
            text = self._revoked.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return float("-inf")
        except OSError:
            return float("inf")  # cannot tell: treat every grant as revoked
        try:
            return float(text)
        except ValueError:
            return float("inf")


__all__ = ["GRANT_TOOLS", "REVOKED_FILE_NAME", "Grant", "GrantStore", "revoke_all", "revoked_file"]

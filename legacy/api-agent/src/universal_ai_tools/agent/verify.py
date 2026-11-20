"""Checks that what the tools reported actually happened, and the task's outcome.

The verifier uses the registry like any other caller: it reads through ``read_file``, so
every workspace rule applies and it has no other access to the filesystem. Its calls are
read-only; they are not the model's and do not count against the model's tool budget.

* A created file is read back. It must exist and have the size ``write_file`` reported,
  and, when it has no characters that ``read_file`` escapes, exactly the content that was
  written.
* A read, listing, or search that returned a result passes; one cut short says so.
* A call that failed or was refused is reported as such, never as done.

``outcome`` turns the task's status and this evidence into ``completed``,
``partially_completed``, ``failed``, or ``cancelled``. ``completed`` needs the model to
have finished, every check to have passed, and no change the task tried to make to have
been refused, denied, or failed. The model's own claims count for nothing here.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from universal_ai_tools.agent.models import (
    Outcome,
    TaskStatus,
    ToolCallRecord,
    Verification,
    VerificationCheck,
)
from universal_ai_tools.filesystem import (
    ListFilesResult,
    ReadFileResult,
    SearchFilesResult,
    WriteFileResult,
)
from universal_ai_tools.policy import escape_line
from universal_ai_tools.providers.models import JsonObject
from universal_ai_tools.tools import ToolCallError, ToolRegistry, ToolRisk
from universal_ai_tools.workspace import WorkspaceError


@dataclass(frozen=True)
class Action:
    """A tool call that ran, with what it was given and returned. Held only during a run."""

    tool_call_id: str
    tool: str
    risk: ToolRisk
    arguments: JsonObject
    result: BaseModel


class Verifier:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def verify(self, actions: list[Action]) -> list[VerificationCheck]:
        return [self._check(action) for action in actions]

    def _check(self, action: Action) -> VerificationCheck:
        result = action.result
        target = _target(action.arguments)
        if isinstance(result, WriteFileResult):
            return self._check_created(action, result, target)

        def passed(check: str, detail: str) -> VerificationCheck:
            return VerificationCheck(
                tool_call_id=action.tool_call_id,
                tool=action.tool,
                target=target,
                check=check,
                passed=True,
                detail=detail,
            )

        if isinstance(result, ReadFileResult):
            return passed("result returned", f"read {result.size_bytes} bytes")
        if isinstance(result, ListFilesResult):
            cut = f", cut short ({result.truncation_reason})" if result.truncated else ""
            return passed("result returned", f"listed {len(result.entries)} entries{cut}")
        if isinstance(result, SearchFilesResult):
            cut = f", cut short ({result.truncation_reason})" if result.truncated else ""
            return passed("result returned", f"found {len(result.matches)} matches{cut}")
        return passed("result returned", "the tool returned a result")

    def _check_created(
        self, action: Action, result: WriteFileResult, target: str | None
    ) -> VerificationCheck:
        def check(passed: bool, detail: str) -> VerificationCheck:
            return VerificationCheck(
                tool_call_id=action.tool_call_id,
                tool=action.tool,
                target=target,
                check="file exists with the written content",
                passed=passed,
                detail=detail,
            )

        path = action.arguments.get("path")
        content = action.arguments.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return check(False, "the call's arguments could not be checked")
        try:
            read = self._registry.invoke("read_file", {"path": path})
        except (WorkspaceError, ToolCallError) as exc:
            return check(False, "reading the file back failed: " + escape_line(str(exc))[:200])
        if not isinstance(read, ReadFileResult):
            return check(False, "reading the file back returned no file content")
        if read.size_bytes != result.bytes_written:
            return check(
                False,
                f"the file has {read.size_bytes} bytes, but {result.bytes_written} were written",
            )
        if read.escaped_characters == 0:
            if read.content != content:
                return check(False, "the file's content differs from what was written")
            return check(True, f"read back {read.size_bytes} bytes; content matches")
        return check(
            True,
            f"read back {read.size_bytes} bytes; size matches (content has escaped "
            "characters, so it was compared by size only)",
        )


def _target(arguments: JsonObject) -> str | None:
    path = arguments.get("path")
    return escape_line(path)[:300] if isinstance(path, str) else None


def summarize(checks: list[VerificationCheck], records: list[ToolCallRecord]) -> Verification:
    failed_checks = sum(not check.passed for check in checks)
    not_done = [r for r in records if r.status != "succeeded"]
    parts = []
    if checks:
        parts.append(f"{len(checks) - failed_checks} of {len(checks)} checks passed")
    else:
        parts.append("no tool call ran, so there was nothing to verify")
    if not_done:
        parts.append(f"{len(not_done)} requested tool calls did not succeed")
    return Verification(checks=tuple(checks), summary="; ".join(parts) + ".")


_INCOMPLETE_CHANGE = frozenset({"denied", "not_approved"})


def outcome(
    status: TaskStatus, checks: list[VerificationCheck], records: list[ToolCallRecord]
) -> Outcome:
    if status is TaskStatus.CANCELLED:
        return "cancelled"
    succeeded = any(r.status == "succeeded" for r in records)
    if status is TaskStatus.BUDGET_EXHAUSTED:
        return "partially_completed" if succeeded else "failed"
    if status is not TaskStatus.COMPLETED:
        return "failed"
    if any(not check.passed for check in checks):
        return "partially_completed"
    if any(r.status in _INCOMPLETE_CHANGE for r in records):
        return "partially_completed"
    if any(r.status == "failed" and r.risk == ToolRisk.CREATE.value for r in records):
        return "partially_completed"
    return "completed"

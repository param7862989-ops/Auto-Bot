"""Records of what an agent task did: steps, tool calls, approvals, usage, and outcome.

These models are metadata for tracking and debugging. They never hold an API key, a
prompt, a tool argument's value other than a path, a tool result, or file content. A path
is kept (escaped and bounded) because it says what an operation touched; a written file's
content is kept only as its size.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RECORD = ConfigDict(frozen=True, extra="forbid")


class TaskStatus(StrEnum):
    """Where a task is in its lifecycle. See ``task.TRANSITIONS`` for the allowed moves."""

    PENDING = "pending"
    PLANNING = "planning"
    """Waiting for the model's next turn."""
    RUNNING = "running"
    """Executing the tool calls of the model's last turn."""
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    """The model finished. Whether its goal was met is the ``Outcome``."""
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    """A hard limit stopped the task. ``TaskFailure.limit`` names it."""


TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BUDGET_EXHAUSTED,
    }
)

Outcome = Literal["completed", "partially_completed", "failed", "cancelled"]
"""How a finished task ended, judged on evidence rather than the model's claims."""

ToolCallStatus = Literal[
    "succeeded",
    "failed",
    "rejected",
    "denied",
    "not_approved",
    "not_run",
]
"""What happened to one requested tool call.

* ``succeeded``: it ran and returned a result.
* ``failed``: it ran and the tool refused or failed (for example, a path that does not exist).
* ``rejected``: it never ran: unknown tool, invalid arguments, or a repeated call.
* ``denied``: the permission policy refused it.
* ``not_approved``: it needed human approval and did not get it.
* ``not_run``: the task stopped (a limit or cancellation) before it ran.
"""

LimitName = Literal[
    "max_steps",
    "max_tool_calls",
    "max_task_seconds",
    "max_input_tokens",
    "max_output_tokens",
    "max_context_chars",
    "max_repeated_calls",
    "max_consecutive_failures",
]

FailureCode = Literal[
    "provider_error",
    "invalid_provider_response",
    "output_truncated",
    "content_filtered",
    "model_stopped",
    "budget_exhausted",
    "cancelled",
    "internal_error",
]


class TaskFailure(BaseModel):
    """Why a task did not complete. ``message`` is fixed text, safe to show."""

    model_config = RECORD

    code: FailureCode
    message: str
    limit: LimitName | None = None


class Usage(BaseModel):
    """Token use summed over a task.

    When a provider does not report a count, an upper bound is used instead and
    ``estimated`` is true: the request's size in characters for input, and the request's
    ``max_output_tokens`` for output.
    """

    model_config = RECORD

    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False
    provider_requests: int = 0


class ApprovalRecord(BaseModel):
    """One request for human approval and its answer."""

    model_config = RECORD

    task_id: str
    tool_call_id: str
    tool: str
    risk: str
    operation: str
    """What was proposed, in words, with the path. Never the content."""
    decision: Literal["approved", "denied", "unavailable", "timed_out", "cancelled", "invalid"]
    decided_at: datetime


class ToolCallRecord(BaseModel):
    """One tool call the model asked for."""

    model_config = RECORD

    step: int
    tool_call_id: str
    tool: str
    """The name the model used, escaped and bounded. May name no registered tool."""
    target: str | None = None
    """The ``path`` argument, if any, escaped and bounded."""
    risk: str | None = None
    permission: Literal["allow", "deny", "needs_approval"] | None = None
    status: ToolCallStatus
    error: str | None = None
    """Why it failed or was refused, safe to show."""
    result_chars: int | None = None
    result_truncated: bool = False


class StepRecord(BaseModel):
    """One model turn."""

    model_config = RECORD

    step: int
    stop_reason: str
    tool_calls: int
    text_chars: int
    input_tokens: int | None
    output_tokens: int | None


class VerificationCheck(BaseModel):
    """One check that an action had the effect it reported."""

    model_config = RECORD

    tool_call_id: str
    tool: str
    target: str | None
    check: str
    passed: bool
    detail: str


class Verification(BaseModel):
    model_config = RECORD

    checks: tuple[VerificationCheck, ...] = ()
    summary: str = Field(description="One line on what the evidence shows.")


class TaskSummary(BaseModel):
    """What a task history keeps: metadata only, without the model's final text."""

    model_config = RECORD

    task_id: str
    objective_chars: int
    provider: str
    model: str
    status: TaskStatus
    outcome: Outcome | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    steps: tuple[StepRecord, ...]
    tool_calls: tuple[ToolCallRecord, ...]
    approvals: tuple[ApprovalRecord, ...]
    usage: Usage
    failure: TaskFailure | None
    verification: Verification | None

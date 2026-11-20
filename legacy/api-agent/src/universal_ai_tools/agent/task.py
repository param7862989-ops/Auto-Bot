"""A task's lifecycle, with explicit transitions, and the in-memory history of tasks.

``Task`` is the live, mutable state of one run. Every status change goes through
``Task.transition``, which refuses any move not in ``TRANSITIONS``; a finished task can
never move again. ``Task.summary`` produces the metadata-only ``TaskSummary`` that a
``TaskHistory`` keeps.

``TaskHistory`` is the seam for persistence. The only implementation is
``InMemoryTaskHistory``: a bounded, process-local record that disappears with the process.
Nothing is written to disk, and a summary holds no prompt, tool result, file content,
final model text, or credential.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from universal_ai_tools.agent.models import (
    TERMINAL_STATUSES,
    ApprovalRecord,
    Outcome,
    StepRecord,
    TaskFailure,
    TaskStatus,
    TaskSummary,
    ToolCallRecord,
    Usage,
    Verification,
)

S = TaskStatus
TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    S.PENDING: frozenset({S.PLANNING, S.FAILED, S.CANCELLED}),
    S.PLANNING: frozenset({S.RUNNING, S.COMPLETED, S.FAILED, S.CANCELLED, S.BUDGET_EXHAUSTED}),
    S.RUNNING: frozenset(
        {S.PLANNING, S.WAITING_FOR_APPROVAL, S.FAILED, S.CANCELLED, S.BUDGET_EXHAUSTED}
    ),
    S.WAITING_FOR_APPROVAL: frozenset({S.RUNNING, S.FAILED, S.CANCELLED, S.BUDGET_EXHAUSTED}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.BUDGET_EXHAUSTED: frozenset(),
}


class InvalidTransitionError(RuntimeError):
    """A status change that the lifecycle does not allow."""


def _now() -> datetime:
    return datetime.now(UTC)


class Task:
    """One agent run: its objective, status, and everything recorded about it."""

    def __init__(self, objective: str, *, provider: str, model: str) -> None:
        self.task_id = f"task_{uuid.uuid4().hex}"
        self.objective = objective
        """Kept only for the run itself; never put into a summary or a log."""
        self.provider = provider
        self.model = model
        self.status = TaskStatus.PENDING
        self.created_at = _now()
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.current_step = 0
        self.steps: list[StepRecord] = []
        self.tool_calls: list[ToolCallRecord] = []
        self.approvals: list[ApprovalRecord] = []
        self.usage = Usage()
        self.final_response: str | None = None
        """The model's last text. Untrusted, and not kept in a summary."""
        self.failure: TaskFailure | None = None
        self.verification: Verification | None = None
        self.outcome: Outcome | None = None

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def transition(self, target: TaskStatus) -> None:
        """Move to ``target``.

        Raises:
            InvalidTransitionError: If the lifecycle does not allow the move.
        """
        if target not in TRANSITIONS[self.status]:
            raise InvalidTransitionError(
                f"task cannot move from {self.status.value} to {target.value}"
            )
        if self.status is TaskStatus.PENDING:
            self.started_at = _now()
        self.status = target
        if target in TERMINAL_STATUSES:
            self.finished_at = _now()

    def summary(self) -> TaskSummary:
        return TaskSummary(
            task_id=self.task_id,
            objective_chars=len(self.objective),
            provider=self.provider,
            model=self.model,
            status=self.status,
            outcome=self.outcome,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            steps=tuple(self.steps),
            tool_calls=tuple(self.tool_calls),
            approvals=tuple(self.approvals),
            usage=self.usage,
            failure=self.failure,
            verification=self.verification,
        )


class TaskHistory(Protocol):
    """Where task summaries are kept. Replace to persist them."""

    def record(self, summary: TaskSummary) -> None: ...

    def get(self, task_id: str) -> TaskSummary | None: ...

    def recent(self) -> tuple[TaskSummary, ...]: ...


class InMemoryTaskHistory:
    """The most recent ``capacity`` summaries, in this process only."""

    def __init__(self, capacity: int = 100) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._items: OrderedDict[str, TaskSummary] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, summary: TaskSummary) -> None:
        with self._lock:
            self._items.pop(summary.task_id, None)
            self._items[summary.task_id] = summary
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def get(self, task_id: str) -> TaskSummary | None:
        with self._lock:
            return self._items.get(task_id)

    def recent(self) -> tuple[TaskSummary, ...]:
        with self._lock:
            return tuple(self._items.values())

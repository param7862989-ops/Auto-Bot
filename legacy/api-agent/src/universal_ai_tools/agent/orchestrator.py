"""The orchestrator: the agent's execution loop, and the only code that runs a model's tool calls.

One task goes through this loop:

1. Start the conversation (system instructions, then the objective) and a ``Budget``.
2. Ask the planner for the model's next turn. Before each request the budget is checked
   (steps, time, tokens, conversation size); after it, tokens are counted and a turn the
   model keeps repeating stops the task.
3. A turn without tool calls ends the task: ``completed`` if the model finished normally,
   ``failed`` if its output was cut off, filtered, or stopped for another reason.
4. Otherwise each tool call is handled in order, one at a time:

   * counted against the tool call limit and the repeated-call limit;
   * looked up in the registry; an unknown name is refused;
   * its arguments validated strictly against the tool's input model, before anything
     else is done with it, and refused if invalid;
   * given the permission policy's decision: run, refuse, or ask a human. Only an explicit
     approval lets a call that needs one run;
   * run through ``ToolRegistry.invoke``, which validates the arguments strictly again
     immediately before dispatch;
   * its result (or why it did not run) given back to the model, cut to the size limit.

   Too many failed or refused calls in a row stop the task.
5. Repeat from 2 until the model finishes, a limit is reached, or the task is cancelled.

Afterwards the verifier checks the effects the tools reported, and the task's outcome is
decided from that evidence. The task's summary goes to the ``TaskHistory``.

**Approval, inline or suspended.** An ``ApprovalChannel`` either answers while the task
waits (``run`` keeps going), or raises ``ApprovalPending`` when the answer can only arrive
in a later request (MCP 2026-07-28, where a server cannot send a request mid-call). Then
the task is *suspended*: ``run`` returns it in ``waiting_for_approval``, its state stays in
memory, and ``resume`` continues it with the human's answer. A suspended task not resumed
within the approval timeout is stopped as cancelled, its pending call not approved.

Limits are enforced here, not by the model. Cancellation is cooperative through a
``CancellationToken``, and immediate through the caller's cancel scope (an MCP client
cancelling the request); either way the task is recorded as ``cancelled``. A tool call that
has started is allowed to finish (shielded) so that its effect is known and recorded.

Nothing is logged but task ids, step numbers, tool names, statuses, and counts.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import anyio
import anyio.to_thread
from pydantic import BaseModel, ValidationError

from universal_ai_tools.agent.budget import Budget, BudgetExhausted, Limits
from universal_ai_tools.agent.config import AgentConfig
from universal_ai_tools.agent.models import (
    ApprovalRecord,
    FailureCode,
    LimitName,
    StepRecord,
    TaskFailure,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from universal_ai_tools.agent.permissions import (
    CONSEQUENCES,
    ApprovalChannel,
    ApprovalDecision,
    ApprovalPending,
    ApprovalRequest,
    Decision,
    decide,
)
from universal_ai_tools.agent.planner import Planner
from universal_ai_tools.agent.task import InMemoryTaskHistory, Task, TaskHistory
from universal_ai_tools.agent.verify import Action, Verifier, outcome, summarize
from universal_ai_tools.policy import escape_line, escape_text
from universal_ai_tools.providers.base import Provider
from universal_ai_tools.providers.errors import ProviderError, ProviderResponseError
from universal_ai_tools.providers.models import (
    AssistantMessage,
    JsonObject,
    Message,
    ProviderResponse,
    ToolCall,
    ToolResultMessage,
)
from universal_ai_tools.tools import ToolCallError, ToolRegistry, ToolSpec
from universal_ai_tools.workspace import WorkspaceError

logger = logging.getLogger(__name__)

_MAX_NAME_CHARS = 64
_MAX_TARGET_CHARS = 300
_MAX_ERROR_CHARS = 300
_PREVIEW_CHARS = 2000
_MAX_REPORTED_ERRORS = 5
MAX_SUSPENDED_TASKS = 4
"""Suspended tasks kept at once. Beyond it, the oldest is stopped."""


class ProgressReporter(Protocol):
    async def __call__(self, step: int, total: int, message: str) -> None: ...


class CancellationToken:
    """Asks a running task to stop at its next checkpoint. Safe to use from any thread."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class _Cancelled(Exception):  # control flow inside a run
    pass


class _Suspend(Exception):  # control flow inside a run
    pass


class _Stop(Exception):  # control flow inside a run
    def __init__(self, status: TaskStatus, failure: TaskFailure | None) -> None:
        super().__init__(failure.message if failure else status.value)
        self.status = status
        self.failure = failure


@dataclass(frozen=True)
class SuspendedApproval:
    """The approval a suspended task waits for."""

    task_id: str
    nonce: str
    """Random; a resume must present it, so a task id alone cannot resume a task."""
    request: ApprovalRequest


class Orchestrator:
    """Runs tasks for one provider over one registry."""

    def __init__(
        self,
        *,
        provider: Provider,
        config: AgentConfig,
        registry: ToolRegistry,
        history: TaskHistory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._config = config
        self._registry = registry
        self._history: TaskHistory = InMemoryTaskHistory() if history is None else history
        self._clock = clock
        self._verifier = Verifier(registry)
        self._suspended: dict[str, _Run] = {}

    @property
    def history(self) -> TaskHistory:
        return self._history

    async def run(
        self,
        objective: str,
        *,
        approval: ApprovalChannel,
        progress: ProgressReporter | None = None,
        cancellation: CancellationToken | None = None,
        max_steps: int | None = None,
        max_tool_calls: int | None = None,
    ) -> Task:
        """Run ``objective`` until it ends or is suspended for approval, and return the task.

        Never raises for a task that failed: the failure is in the task. Re-raises the
        caller's own cancellation after recording it. A task returned in
        ``waiting_for_approval`` is suspended; see ``suspended`` and ``resume``.
        """
        await self.expire()
        task = Task(objective, provider=self._provider.name, model=self._config.model)
        limits = Limits.from_config(
            self._config, max_steps=max_steps, max_tool_calls=max_tool_calls
        )
        run = _Run(
            task=task,
            budget=Budget(limits, clock=self._clock),
            planner=Planner(self._provider, self._config.model, self._registry),
            registry=self._registry,
            approval=approval,
            approval_timeout=self._config.approval_timeout_seconds,
            progress=progress,
            cancellation=cancellation or CancellationToken(),
            clock=self._clock,
        )
        logger.info("task %s: started (provider %s)", task.task_id, task.provider)
        await self._drive(run, None)
        return task

    def suspended(self, task_id: str) -> SuspendedApproval | None:
        """The approval a suspended task is waiting for, if it is suspended."""
        run = self._suspended.get(task_id)
        if run is None or run.pending is None:
            return None
        return SuspendedApproval(
            task_id=task_id, nonce=run.pending.nonce, request=run.pending.request
        )

    async def resume(
        self,
        task_id: str,
        nonce: str,
        answer: ApprovalDecision,
        *,
        approval: ApprovalChannel,
        progress: ProgressReporter | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Task | None:
        """Continue a suspended task with the human's ``answer``.

        Returns ``None`` if no task with this id and nonce is suspended (unknown, already
        resumed, or expired). An answer that arrives after the approval timeout counts as
        ``TIMED_OUT``, whatever it says.
        """
        await self.expire()
        run = self._suspended.get(task_id)
        if (
            run is None
            or run.pending is None
            or not secrets.compare_digest(run.pending.nonce, nonce)
        ):
            return None
        del self._suspended[task_id]
        if self._clock() - run.pending.asked_at > run.approval_timeout:
            answer = ApprovalDecision.TIMED_OUT
        run.approval = approval
        run.progress = progress
        run.cancellation = cancellation or CancellationToken()
        await self._drive(run, answer)
        return run.task

    async def expire(self) -> None:
        """Stop suspended tasks whose approval was not answered in time."""
        now = self._clock()
        expired = [
            run
            for run in self._suspended.values()
            if run.pending is not None and now - run.pending.asked_at > run.approval_timeout
        ]
        while len(self._suspended) - len(expired) > MAX_SUSPENDED_TASKS - 1:
            oldest = min(
                (r for r in self._suspended.values() if r not in expired),
                key=lambda r: r.pending.asked_at if r.pending else 0.0,
            )
            expired.append(oldest)
        for run in expired:
            self._suspended.pop(run.task.task_id, None)
            run.abandon()
            await self._finalize(run)

    async def _drive(self, run: _Run, answer: ApprovalDecision | None) -> None:
        try:
            await run.execute(answer)
        except anyio.get_cancelled_exc_class():
            with anyio.CancelScope(shield=True):
                run.stop(TaskStatus.CANCELLED, _failure("cancelled", "the task was cancelled"))
                await self._finalize(run)
            raise
        if run.pending is not None and not run.task.finished:
            self._suspended[run.task.task_id] = run
            logger.info("task %s: suspended for approval", run.task.task_id)
            return
        await self._finalize(run)

    async def _finalize(self, run: _Run) -> None:
        task = run.task
        checks = await anyio.to_thread.run_sync(self._verifier.verify, run.actions)
        task.verification = summarize(checks, task.tool_calls)
        task.outcome = outcome(task.status, checks, task.tool_calls)
        task.usage = run.budget.usage()
        self._history.record(task.summary())
        logger.info(
            "task %s: %s (%s) after %d steps and %d tool calls",
            task.task_id,
            task.status.value,
            task.outcome,
            len(task.steps),
            len(task.tool_calls),
        )


@dataclass
class _Turn:
    """The tool calls of one model turn, and how far handling them has got."""

    response: ProviderResponse
    index: int = 0
    results: list[ToolResultMessage] = field(default_factory=list)


@dataclass
class _Pending:
    turn: _Turn
    call: ToolCall
    spec: ToolSpec[Any, Any]
    request: ApprovalRequest
    asked_at: float
    nonce: str


class _Run:
    """The state of one task while it runs or is suspended."""

    def __init__(
        self,
        *,
        task: Task,
        budget: Budget,
        planner: Planner,
        registry: ToolRegistry,
        approval: ApprovalChannel,
        approval_timeout: float,
        progress: ProgressReporter | None,
        cancellation: CancellationToken,
        clock: Callable[[], float],
    ) -> None:
        self.task = task
        self.budget = budget
        self.planner = planner
        self.registry = registry
        self.approval = approval
        self.approval_timeout = approval_timeout
        self.progress = progress
        self.cancellation = cancellation
        self.clock = clock
        self.actions: list[Action] = []
        self.messages: list[Message] = planner.start(task.objective)
        self.seen_call_ids: set[str] = set()
        self.tools_chars = sum(len(tool.model_dump_json()) for tool in planner.tools)
        self.pending: _Pending | None = None

    # Lifecycle

    def stop(self, status: TaskStatus, failure: TaskFailure | None) -> None:
        if self.task.finished:
            return
        if self.pending is not None:
            self._settle_pending(ApprovalDecision.CANCELLED)
        self.task.failure = failure
        self.task.transition(status)

    def abandon(self) -> None:
        """Stop a suspended task whose approval never came."""
        if self.pending is not None:
            self._settle_pending(ApprovalDecision.TIMED_OUT)
        self.stop(
            TaskStatus.CANCELLED,
            _failure("cancelled", "the approval request was not answered, so the task stopped"),
        )

    def _settle_pending(self, answer: ApprovalDecision) -> None:
        """Record the answer to a pending approval that the task will not act on."""
        pending = self.pending
        if pending is None:
            return
        self.pending = None
        self._record_approval(pending.call, pending.spec, pending.request, answer)
        self._record(
            pending.call,
            "not_approved",
            error=f"the user did not approve this call ({answer.value})",
            spec=pending.spec,
            permission=Decision.NEEDS_APPROVAL,
        )
        self._mark_not_run(pending.turn.response)

    async def execute(self, answer: ApprovalDecision | None = None) -> None:
        try:
            with anyio.move_on_after(self.budget.remaining_seconds()) as deadline:
                if answer is not None:
                    await self._resume(answer)
                await self._loop()
            if deadline.cancelled_caught and not self.task.finished:
                raise BudgetExhausted("max_task_seconds", "the task ran out of time")
        except _Suspend:
            return
        except _Stop as stop:
            self.stop(stop.status, stop.failure)
        except BudgetExhausted as exc:
            self.stop(
                TaskStatus.BUDGET_EXHAUSTED,
                _failure("budget_exhausted", str(exc), limit=exc.limit),
            )
        except _Cancelled:
            self.stop(TaskStatus.CANCELLED, _failure("cancelled", "the task was cancelled"))
        except ProviderResponseError as exc:
            self.stop(TaskStatus.FAILED, _failure("invalid_provider_response", str(exc)))
        except ProviderError as exc:
            self.stop(TaskStatus.FAILED, _failure("provider_error", str(exc)))
        except Exception as exc:
            logger.error("task %s: internal error %s", self.task.task_id, type(exc).__name__)
            self.stop(
                TaskStatus.FAILED,
                _failure("internal_error", "the task stopped because of an internal error"),
            )

    def _checkpoint(self) -> None:
        if self.cancellation.cancelled:
            raise _Cancelled()

    async def _report(self, message: str) -> None:
        if self.progress is None:
            return
        try:
            await self.progress(self.budget.steps, self.budget.limits.max_steps, message)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:
            logger.debug("progress report failed: %s", type(exc).__name__)

    # The loop

    async def _loop(self) -> None:
        while True:
            self._checkpoint()
            self.task.transition(TaskStatus.PLANNING)
            context = _conversation_chars(self.messages)
            max_output = self.budget.before_request(context)
            self.task.current_step = self.budget.steps
            await self._report("waiting for the model")
            request = self.planner.request(self.messages, max_output)
            response = await self.planner.next_turn(request)
            self._record_step(response)
            self.budget.after_response(
                response,
                request_chars=context + self.tools_chars,
                max_output_tokens=max_output,
            )
            self.task.usage = self.budget.usage()
            if not response.tool_calls:
                self._finish_turn(response)
                return
            ids = [call.id for call in response.tool_calls]
            if self.seen_call_ids.intersection(ids):
                raise ProviderResponseError("the model provider reused a tool call id")
            self.seen_call_ids.update(ids)
            self.messages.append(
                AssistantMessage(content=response.content, tool_calls=response.tool_calls)
            )
            self.task.transition(TaskStatus.RUNNING)
            await self._continue(_Turn(response))

    def _record_step(self, response: ProviderResponse) -> None:
        self.task.steps.append(
            StepRecord(
                step=self.budget.steps,
                stop_reason=response.stop_reason,
                tool_calls=len(response.tool_calls),
                text_chars=len(response.content),
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        )

    def _finish_turn(self, response: ProviderResponse) -> None:
        self.task.final_response = response.content
        reason = response.stop_reason
        if reason in ("end_turn", "stop_sequence"):
            raise _Stop(TaskStatus.COMPLETED, None)
        if reason == "max_tokens":
            failure = _failure("output_truncated", "the model's answer was cut off at its limit")
        elif reason == "content_filter":
            failure = _failure("content_filtered", "the provider withheld the model's answer")
        else:
            failure = _failure("model_stopped", "the model stopped without finishing")
        raise _Stop(TaskStatus.FAILED, failure)

    async def _continue(self, turn: _Turn, answer: ApprovalDecision | None = None) -> None:
        """Handle the calls of ``turn`` from ``turn.index`` on, in order.

        With ``answer``, the call at ``turn.index`` is the one a suspended approval was
        about, and it is settled by ``answer`` first.
        """
        calls = turn.response.tool_calls
        try:
            if answer is not None and self.pending is not None:
                pending, self.pending = self.pending, None
                self._record_approval(pending.call, pending.spec, pending.request, answer)
                self.task.transition(TaskStatus.RUNNING)
                turn.results.append(await self._settle(pending.call, pending.spec, answer))
                turn.index += 1
            while turn.index < len(calls):
                self._checkpoint()
                turn.results.append(await self._handle(calls[turn.index], turn))
                turn.index += 1
        except _Suspend:
            raise
        except BaseException:
            self._mark_not_run(turn.response)
            raise
        self.messages.extend(turn.results)

    async def _resume(self, answer: ApprovalDecision) -> None:
        pending = self.pending
        if pending is None:
            return
        await self._continue(pending.turn, answer)

    def _mark_not_run(self, response: ProviderResponse) -> None:
        """Record every call of ``response`` that has no record yet as not run."""
        recorded = {record.tool_call_id for record in self.task.tool_calls}
        for call in response.tool_calls:
            if call.id not in recorded:
                self._record(call, "not_run", error="the task stopped before this call ran")

    # One tool call

    async def _handle(self, call: ToolCall, turn: _Turn) -> ToolResultMessage:
        try:
            self.budget.admit_tool_call(call)
        except BudgetExhausted as exc:
            status: ToolCallStatus = "rejected" if exc.limit == "max_repeated_calls" else "not_run"
            self._record(call, status, error=str(exc))
            raise

        decision, spec = decide(self.registry, call.name)
        if spec is None:
            return self._refuse(call, "rejected", "unknown tool: no tool of that name exists")
        try:
            spec.input_model.model_validate(dict(call.arguments), strict=True)
        except ValidationError as exc:
            return self._refuse(call, "rejected", _describe(spec.name, exc), spec=spec)
        if decision is Decision.ALLOW:
            return await self._execute(call, spec, decision)
        if decision is Decision.NEEDS_APPROVAL:
            answer = await self._ask(call, spec, turn)
            self.task.transition(TaskStatus.RUNNING)
            return await self._settle(call, spec, answer)
        return self._refuse(
            call, "denied", "the permission policy does not allow this tool", spec=spec
        )

    async def _settle(
        self, call: ToolCall, spec: ToolSpec[Any, Any], answer: ApprovalDecision
    ) -> ToolResultMessage:
        if answer is ApprovalDecision.APPROVED:
            return await self._execute(call, spec, Decision.NEEDS_APPROVAL)
        return self._refuse(
            call,
            "not_approved",
            f"the user did not approve this call ({answer.value}); do not repeat it",
            spec=spec,
            permission=Decision.NEEDS_APPROVAL,
        )

    async def _ask(self, call: ToolCall, spec: ToolSpec[Any, Any], turn: _Turn) -> ApprovalDecision:
        request = _approval_request(self.task.task_id, call, spec)
        self.task.transition(TaskStatus.WAITING_FOR_APPROVAL)
        await self._report(f"waiting for approval of {spec.name}")
        answer = ApprovalDecision.INVALID
        try:
            with anyio.move_on_after(self.approval_timeout) as waited:
                answer = await self.approval.request_approval(request)
            if waited.cancelled_caught:
                answer = ApprovalDecision.TIMED_OUT
            elif not isinstance(answer, ApprovalDecision):
                answer = ApprovalDecision.INVALID
        except ApprovalPending:
            self.pending = _Pending(
                turn=turn,
                call=call,
                spec=spec,
                request=request,
                asked_at=self.clock(),
                nonce=secrets.token_urlsafe(24),
            )
            raise _Suspend() from None
        except anyio.get_cancelled_exc_class():
            self._record_approval(call, spec, request, ApprovalDecision.CANCELLED)
            raise
        except Exception as exc:
            logger.warning("approval channel failed: %s", type(exc).__name__)
            answer = ApprovalDecision.INVALID
        self._record_approval(call, spec, request, answer)
        return answer

    def _record_approval(
        self,
        call: ToolCall,
        spec: ToolSpec[Any, Any],
        request: ApprovalRequest,
        answer: ApprovalDecision,
    ) -> None:
        self.task.approvals.append(
            ApprovalRecord(
                task_id=self.task.task_id,
                tool_call_id=call.id,
                tool=spec.name,
                risk=spec.risk.value,
                operation=request.operation,
                decision=answer.value,
                decided_at=_now(),
            )
        )

    async def _execute(
        self, call: ToolCall, spec: ToolSpec[Any, Any], decision: Decision
    ) -> ToolResultMessage:
        await self._report(f"running {spec.name}")
        arguments = dict(call.arguments)
        error: str | None = None
        result: BaseModel | None = None
        # A call that has started finishes, so its effect is known and recorded.
        with anyio.CancelScope(shield=True):
            try:
                result = await anyio.to_thread.run_sync(self.registry.invoke, spec.name, arguments)
            except (WorkspaceError, ToolCallError) as exc:
                error = escape_line(str(exc))[:_MAX_ERROR_CHARS]
            except Exception as exc:
                logger.warning("tool %s failed: %s", spec.name, type(exc).__name__)
                error = "the tool failed unexpectedly"
        if result is None:
            self._record(call, "failed", error=error, spec=spec, permission=decision)
            self.budget.tool_call_finished(ok=False)
            return ToolResultMessage(tool_call_id=call.id, content=f"Error: {error}", is_error=True)

        self.actions.append(
            Action(
                tool_call_id=call.id,
                tool=spec.name,
                risk=spec.risk,
                arguments=call.arguments,
                result=result,
            )
        )
        text = result.model_dump_json()
        limit = self.budget.limits.max_tool_result_chars
        truncated = len(text) > limit
        if truncated:
            text = text[:limit] + f"\n[result cut: {len(text)} characters, limit {limit}]"
        self._record(
            call,
            "succeeded",
            spec=spec,
            permission=decision,
            result_chars=len(text),
            truncated=truncated,
        )
        self.budget.tool_call_finished(ok=True)
        return ToolResultMessage(tool_call_id=call.id, content=text)

    def _refuse(
        self,
        call: ToolCall,
        status: ToolCallStatus,
        reason: str,
        *,
        spec: ToolSpec[Any, Any] | None = None,
        permission: Decision | None = None,
    ) -> ToolResultMessage:
        if permission is None and status == "denied":
            permission = Decision.DENY
        self._record(call, status, error=reason, spec=spec, permission=permission)
        message = ToolResultMessage(
            tool_call_id=call.id, content=f"Refused: {reason}", is_error=True
        )
        self.budget.tool_call_finished(ok=False)
        return message

    def _record(
        self,
        call: ToolCall,
        status: ToolCallStatus,
        *,
        error: str | None = None,
        spec: ToolSpec[Any, Any] | None = None,
        permission: Decision | None = None,
        result_chars: int | None = None,
        truncated: bool = False,
    ) -> None:
        self.task.tool_calls.append(
            ToolCallRecord(
                step=self.budget.steps,
                tool_call_id=call.id,
                tool=escape_line(call.name)[:_MAX_NAME_CHARS],
                target=_target(call.arguments),
                risk=spec.risk.value if spec is not None else None,
                permission=permission.value if permission is not None else None,
                status=status,
                error=error,
                result_chars=result_chars,
                result_truncated=truncated,
            )
        )


# Helpers


def _failure(code: FailureCode, message: str, *, limit: LimitName | None = None) -> TaskFailure:
    return TaskFailure(code=code, message=message, limit=limit)


def _now() -> datetime:
    return datetime.now(UTC)


def _target(arguments: JsonObject) -> str | None:
    path = arguments.get("path")
    return escape_line(path)[:_MAX_TARGET_CHARS] if isinstance(path, str) else None


def _conversation_chars(messages: Sequence[Message]) -> int:
    total = 0
    for message in messages:
        total += len(message.content)
        if isinstance(message, AssistantMessage):
            total += sum(len(call.model_dump_json()) for call in message.tool_calls)
    return total


def _describe(name: str, exc: ValidationError) -> str:
    """What is wrong with the arguments, without quoting any argument value."""
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    problems = [
        "{}: {}".format(
            ".".join(escape_line(str(part))[:_MAX_NAME_CHARS] for part in error["loc"])
            or "(arguments)",
            error["msg"],
        )
        for error in errors[:_MAX_REPORTED_ERRORS]
    ]
    if len(errors) > _MAX_REPORTED_ERRORS:
        problems.append(f"and {len(errors) - _MAX_REPORTED_ERRORS} more")
    return f"invalid arguments for '{name}': " + "; ".join(problems)


def _approval_request(task_id: str, call: ToolCall, spec: ToolSpec[Any, Any]) -> ApprovalRequest:
    target = _target(call.arguments)
    details: list[tuple[str, str]] = []
    if target is not None:
        details.append(("Path", target))
    for key, value in call.arguments.items():
        if key == "path":
            continue
        label = escape_line(key)[:_MAX_NAME_CHARS]
        if isinstance(value, str):
            size = len(value.encode("utf-8", errors="replace"))
            preview, _ = escape_text(value[:_PREVIEW_CHARS])
            more = "" if len(value) <= _PREVIEW_CHARS else f"\n[... {len(value)} characters in all]"
            details.append((f"{label} ({size} bytes)", preview + more))
        else:
            details.append((label, escape_line(str(value))[:_MAX_TARGET_CHARS]))
    where = f" '{target}'" if target is not None else ""
    return ApprovalRequest(
        task_id=task_id,
        tool_call_id=call.id,
        tool=spec.name,
        risk=spec.risk.value,
        operation=f"{spec.title}{where}",
        consequences=CONSEQUENCES.get(
            spec.risk, "This tool can change the workspace. Approve only if you expect it."
        ),
        details=tuple(details),
    )

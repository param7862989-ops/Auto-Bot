"""Task lifecycle, task history, budgets, and the permission policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_support import SECRET, agent_config, call, make_workspace, registry
from universal_ai_tools.agent.budget import Budget, BudgetExhausted, Limits
from universal_ai_tools.agent.models import TERMINAL_STATUSES, TaskStatus
from universal_ai_tools.agent.permissions import (
    ApprovalDecision,
    Decision,
    DeferredApprovalChannel,
    UnavailableApprovalChannel,
    decide,
)
from universal_ai_tools.agent.task import (
    TRANSITIONS,
    InMemoryTaskHistory,
    InvalidTransitionError,
    Task,
)
from universal_ai_tools.providers import TokenUsage, text_response, tool_call_response
from universal_ai_tools.tools import ToolRisk

S = TaskStatus

# Lifecycle


def test_new_task() -> None:
    task = Task("objective", provider="fake", model="m")
    assert task.status is S.PENDING
    assert task.task_id.startswith("task_")
    assert task.started_at is None
    assert Task("x", provider="p", model="m").task_id != task.task_id


def test_a_normal_lifecycle() -> None:
    task = Task("objective", provider="fake", model="m")
    for status in (S.PLANNING, S.RUNNING, S.WAITING_FOR_APPROVAL, S.RUNNING, S.PLANNING):
        task.transition(status)
    task.transition(S.COMPLETED)

    assert task.finished
    assert task.started_at is not None
    assert task.finished_at is not None


def every_pair() -> list[tuple[TaskStatus, TaskStatus]]:
    return [(a, b) for a in TaskStatus for b in TaskStatus]


@pytest.mark.parametrize(("source", "target"), every_pair())
def test_transitions_follow_the_table(source: TaskStatus, target: TaskStatus) -> None:
    task = Task("x", provider="p", model="m")
    task.status = source

    if target in TRANSITIONS[source]:
        task.transition(target)
        assert task.status is target
    else:
        with pytest.raises(InvalidTransitionError):
            task.transition(target)
        assert task.status is source


def test_terminal_states_are_final() -> None:
    for status in TERMINAL_STATUSES:
        assert TRANSITIONS[status] == frozenset()


def test_completion_needs_the_model() -> None:
    # Only a model turn (planning) can complete a task; running tools cannot.
    assert S.COMPLETED in TRANSITIONS[S.PLANNING]
    assert S.COMPLETED not in TRANSITIONS[S.RUNNING]
    assert S.COMPLETED not in TRANSITIONS[S.WAITING_FOR_APPROVAL]


def test_summary_holds_no_objective_or_final_text() -> None:
    task = Task(f"objective with {SECRET}", provider="fake", model="m")
    task.final_response = "final text CANARY"
    task.transition(S.PLANNING)
    task.transition(S.COMPLETED)

    dumped = task.summary().model_dump_json()

    assert SECRET not in dumped
    assert "CANARY" not in dumped
    assert task.summary().objective_chars == len(task.objective)


# History


def test_history_is_bounded_and_ordered() -> None:
    history = InMemoryTaskHistory(capacity=2)
    tasks = [Task("x", provider="p", model="m") for _ in range(3)]
    for task in tasks:
        history.record(task.summary())

    assert [s.task_id for s in history.recent()] == [tasks[1].task_id, tasks[2].task_id]
    assert history.get(tasks[0].task_id) is None
    assert history.get(tasks[2].task_id) is not None


def test_history_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        InMemoryTaskHistory(capacity=0)


# Budget


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def limits(**overrides: object) -> Limits:
    return Limits.from_config(agent_config(**overrides))


def test_limits_can_only_be_lowered() -> None:
    config = agent_config(max_steps=10, max_tool_calls=20)

    assert Limits.from_config(config, max_steps=3).max_steps == 3
    assert Limits.from_config(config, max_steps=50).max_steps == 10
    assert Limits.from_config(config, max_tool_calls=99).max_tool_calls == 20
    with pytest.raises(ValueError, match="at least 1"):
        Limits.from_config(config, max_steps=0)


def test_step_limit() -> None:
    budget = Budget(limits(max_steps=2))
    budget.before_request(0)
    budget.before_request(0)

    with pytest.raises(BudgetExhausted) as excinfo:
        budget.before_request(0)
    assert excinfo.value.limit == "max_steps"


def test_time_limit() -> None:
    clock = Clock()
    budget = Budget(limits(max_task_seconds=10), clock=clock)
    budget.before_request(0)
    clock.now = 10.0

    with pytest.raises(BudgetExhausted) as excinfo:
        budget.before_request(0)
    assert excinfo.value.limit == "max_task_seconds"


def test_context_limit() -> None:
    budget = Budget(limits(max_context_chars=1000))
    budget.before_request(1000)

    with pytest.raises(BudgetExhausted) as excinfo:
        budget.before_request(1001)
    assert excinfo.value.limit == "max_context_chars"


def test_token_limits_use_reported_usage() -> None:
    budget = Budget(limits(max_input_tokens=100, max_output_tokens=50))
    usage = TokenUsage(input_tokens=60, output_tokens=10)
    budget.before_request(0)
    budget.after_response(text_response("a", usage=usage), request_chars=0, max_output_tokens=50)
    budget.before_request(0)
    budget.after_response(text_response("b", usage=usage), request_chars=0, max_output_tokens=50)

    with pytest.raises(BudgetExhausted) as excinfo:
        budget.before_request(0)
    assert excinfo.value.limit == "max_input_tokens"
    assert budget.usage().estimated is False


def test_missing_usage_is_counted_as_an_upper_bound() -> None:
    budget = Budget(limits(max_output_tokens=100, max_output_tokens_per_request=60))
    max_output = budget.before_request(0)
    assert max_output == 60

    budget.after_response(text_response("a"), request_chars=500, max_output_tokens=max_output)

    usage = budget.usage()
    assert (usage.input_tokens, usage.output_tokens, usage.estimated) == (500, 60, True)
    # Only 40 output tokens remain, so the next request asks for no more than that.
    assert budget.before_request(0) == 40


def test_output_budget_exhausted() -> None:
    budget = Budget(limits(max_output_tokens=10))
    budget.before_request(0)
    budget.after_response(
        text_response("a", usage=TokenUsage(input_tokens=1, output_tokens=10)),
        request_chars=0,
        max_output_tokens=10,
    )
    with pytest.raises(BudgetExhausted) as excinfo:
        budget.before_request(0)
    assert excinfo.value.limit == "max_output_tokens"


def test_tool_call_limit() -> None:
    budget = Budget(limits(max_tool_calls=2))
    budget.admit_tool_call(call("a", "list_files", path="a"))
    budget.admit_tool_call(call("b", "list_files", path="b"))

    with pytest.raises(BudgetExhausted) as excinfo:
        budget.admit_tool_call(call("c", "list_files", path="c"))
    assert excinfo.value.limit == "max_tool_calls"


def test_repeated_identical_calls() -> None:
    budget = Budget(limits(max_repeated_calls=2))
    budget.admit_tool_call(call("a", "read_file", path="x"))
    budget.admit_tool_call(call("b", "read_file", path="y"))
    budget.admit_tool_call(call("c", "read_file", path="x"))

    with pytest.raises(BudgetExhausted) as excinfo:
        budget.admit_tool_call(call("d", "read_file", path="x"))
    assert excinfo.value.limit == "max_repeated_calls"


def test_argument_order_does_not_hide_a_repeat() -> None:
    budget = Budget(limits(max_repeated_calls=1))
    budget.admit_tool_call(call("a", "search_files", pattern="p", path="."))

    with pytest.raises(BudgetExhausted):
        budget.admit_tool_call(call("b", "search_files", path=".", pattern="p"))


def test_repeated_turns() -> None:
    budget = Budget(limits(max_repeated_calls=2))
    for index in range(2):
        budget.before_request(0)
        budget.after_response(
            tool_call_response(call(f"id{index}", "list_files")),
            request_chars=0,
            max_output_tokens=10,
        )
    budget.before_request(0)
    with pytest.raises(BudgetExhausted) as excinfo:
        budget.after_response(
            tool_call_response(call("id9", "list_files")), request_chars=0, max_output_tokens=10
        )
    assert excinfo.value.limit == "max_repeated_calls"


def test_consecutive_failures() -> None:
    budget = Budget(limits(max_consecutive_failures=3))
    budget.tool_call_finished(ok=False)
    budget.tool_call_finished(ok=False)
    budget.tool_call_finished(ok=True)
    budget.tool_call_finished(ok=False)
    budget.tool_call_finished(ok=False)

    with pytest.raises(BudgetExhausted) as excinfo:
        budget.tool_call_finished(ok=False)
    assert excinfo.value.limit == "max_consecutive_failures"


# Permissions


def test_read_only_tools_are_allowed(tmp_path: Path) -> None:
    tools = registry(make_workspace(tmp_path))
    for name in ("get_platform_info", "list_files", "read_file", "search_files"):
        decision, spec = decide(tools, name)
        assert decision is Decision.ALLOW
        assert spec is not None
        assert spec.risk is ToolRisk.READ_ONLY


def test_write_file_is_denied_when_write_tools_are_off(tmp_path: Path) -> None:
    decision, spec = decide(registry(make_workspace(tmp_path)), "write_file")
    assert (decision, spec) == (Decision.DENY, None)


def test_write_file_needs_approval_when_write_tools_are_on(tmp_path: Path) -> None:
    decision, spec = decide(registry(make_workspace(tmp_path), write=True), "write_file")
    assert decision is Decision.NEEDS_APPROVAL
    assert spec is not None


@pytest.mark.parametrize(
    "name", ["run_command", "run_task", "delete_file", "", "READ_FILE", "read_file ", 5, None]
)
def test_unknown_operations_are_denied(tmp_path: Path, name: object) -> None:
    decision, spec = decide(registry(make_workspace(tmp_path), write=True), name)  # type: ignore[arg-type]
    assert (decision, spec) == (Decision.DENY, None)


def test_an_unhandled_risk_is_denied(tmp_path: Path) -> None:
    tools = registry(make_workspace(tmp_path), write=True)
    spec = tools.get("write_file")
    # ToolSpec is frozen; this stands in for a risk added later and not handled by decide.
    object.__setattr__(spec, "risk", "a-future-risk")
    try:
        decision, _ = decide(tools, "write_file")
    finally:
        object.__setattr__(spec, "risk", ToolRisk.CREATE)
    assert decision is Decision.DENY


@pytest.mark.anyio
async def test_unavailable_channel_never_approves() -> None:
    from universal_ai_tools.agent.permissions import ApprovalRequest

    request = ApprovalRequest(
        task_id="t",
        tool_call_id="c",
        tool="write_file",
        risk="create",
        operation="op",
        consequences="c",
        details=(),
    )
    assert await UnavailableApprovalChannel().request_approval(request) is (
        ApprovalDecision.UNAVAILABLE
    )
    from universal_ai_tools.agent.permissions import ApprovalPending

    with pytest.raises(ApprovalPending):
        await DeferredApprovalChannel().request_approval(request)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"

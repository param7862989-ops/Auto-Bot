"""``run_task`` over MCP: registration, approval by elicitation, results, and the entry point.

Approval is tested over both protocol generations. ``Client(server)`` negotiates MCP
2026-07-28, where a server cannot send a request mid-call: ``run_task`` suspends the task
and returns ``InputRequiredResult``, and the SDK client asks (``elicitation_callback``)
and retries. ``Client(server, mode="legacy")`` uses the initialize handshake (2025-11-25),
where the server sends ``elicitation/create`` while the task waits.
"""

from __future__ import annotations

import builtins
import json
import logging
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.session import ElicitationFnT
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    ElicitRequestFormParams,
    ElicitRequestParams,
    ElicitResult,
    TextContent,
)

from agent_support import SECRET, agent_config, call, make_workspace
from universal_ai_tools.agent.config import (
    ENV_AGENT_MODEL,
    ENV_AGENT_PROVIDER,
    ENV_ANTHROPIC_API_KEY,
    ENV_ENABLE_AGENT_TOOLS,
)
from universal_ai_tools.agent.mcp_tool import AgentSetup, register_run_task
from universal_ai_tools.agent.models import TaskStatus
from universal_ai_tools.config import ENV_ENABLE_WRITE_TOOLS, ENV_WORKSPACE, AppConfig, load_config
from universal_ai_tools.providers import (
    FakeProvider,
    FakeStep,
    Provider,
    ProviderAuthenticationError,
    ProviderRequest,
    ProviderResponse,
    text_response,
    tool_call_response,
)
from universal_ai_tools.server import (
    READ_ONLY_INSTRUCTIONS,
    WRITE_ENABLED_INSTRUCTIONS,
    create_server,
    main,
)
from universal_ai_tools.tools import build_registry

DEFAULT_TOOLS = ["get_platform_info", "list_files", "read_file", "search_files"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return make_workspace(tmp_path)


def app_config(workspace: Path, *, write: bool = False) -> AppConfig:
    environ = {ENV_WORKSPACE: str(workspace)}
    if write:
        environ[ENV_ENABLE_WRITE_TOOLS] = "true"
    return load_config(environ)


def setup(provider: Provider, **config: Any) -> AgentSetup:
    return AgentSetup(config=agent_config(**config), provider=provider)


def answering(*answers: ElicitResult) -> tuple[ElicitationFnT, list[ElicitRequestParams]]:
    queue = list(answers)
    asked: list[ElicitRequestParams] = []

    async def elicit(context: Any, params: ElicitRequestParams) -> ElicitResult:
        asked.append(params)
        return queue.pop(0)

    return elicit, asked


APPROVE = ElicitResult(action="accept", content={"decision": "approve"})
DENY = ElicitResult(action="accept", content={"decision": "deny"})

WRITE_SCRIPT: list[FakeStep] = [
    tool_call_response(call("w1", "write_file", path="docs/new.txt", content="made by agent\n")),
    text_response("Created it."),
]


def structured(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


# Registration


@pytest.mark.anyio
@pytest.mark.parametrize("write", [False, True])
async def test_agent_disabled_by_default(workspace: Path, write: bool) -> None:
    async with Client(create_server(app_config(workspace, write=write))) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        assert "run_task" not in names
        assert client.instructions == (
            WRITE_ENABLED_INSTRUCTIONS if write else READ_ONLY_INSTRUCTIONS
        )
        result = await client.call_tool("run_task", {"objective": "x"})
    assert result.is_error


@pytest.mark.anyio
@pytest.mark.parametrize("write", [False, True])
async def test_agent_enabled_adds_only_run_task(workspace: Path, write: bool) -> None:
    config = app_config(workspace, write=write)
    async with Client(create_server(config)) as plain:
        before = [tool.model_dump() for tool in (await plain.list_tools()).tools]
        plain_instructions = plain.instructions

    agent = setup(FakeProvider([]))
    async with Client(create_server(config, agent=agent)) as client:
        after = [tool.model_dump() for tool in (await client.list_tools()).tools]
        instructions = client.instructions

    assert after[:-1] == before
    run_task = after[-1]
    assert run_task["name"] == "run_task"
    assert run_task["annotations"]["read_only_hint"] is (not write)
    assert run_task["annotations"]["destructive_hint"] is False
    assert run_task["annotations"]["open_world_hint"] is True
    assert instructions is not None
    assert plain_instructions is not None
    assert instructions.startswith(plain_instructions)
    assert "run_task" in instructions[len(plain_instructions) :]
    description = run_task["description"]
    assert "cannot run commands" in description
    assert ("approval" in description) is write
    assert SECRET not in json.dumps(after) + instructions


@pytest.mark.anyio
async def test_run_task_schema_exposes_no_configuration(workspace: Path) -> None:
    async with Client(create_server(app_config(workspace), agent=setup(FakeProvider([])))) as c:
        tool = next(t for t in (await c.list_tools()).tools if t.name == "run_task")
    assert set(tool.input_schema["properties"]) == {"objective", "max_steps", "max_tool_calls"}
    assert tool.input_schema["required"] == ["objective"]


# Running tasks


@pytest.mark.anyio
async def test_task_completion(workspace: Path) -> None:
    provider = FakeProvider(
        [
            tool_call_response(call("c1", "read_file", path="docs/hello.txt")),
            text_response("The file says hello world."),
        ]
    )
    async with Client(create_server(app_config(workspace), agent=setup(provider))) as client:
        result = structured(await client.call_tool("run_task", {"objective": "Read hello."}))

    assert result["status"] == "completed"
    assert result["outcome"] == "completed"
    assert result["requested"] == "Read hello."
    assert result["final_response"] == "The file says hello world."
    assert result["executed"] == [
        {"tool": "read_file", "target": "docs/hello.txt", "status": "succeeded", "error": None}
    ]
    assert result["verification"][0]["passed"] is True
    assert result["incomplete"] == []
    assert (result["steps_used"], result["tool_calls_used"]) == (2, 1)
    assert result["usage"]["provider_requests"] == 2
    assert result["failure"] is None
    assert result["trust"] == "untrusted"


@pytest.mark.anyio
async def test_task_failure_is_safe(workspace: Path) -> None:
    provider = FakeProvider([ProviderAuthenticationError()])
    async with Client(create_server(app_config(workspace), agent=setup(provider))) as client:
        raw = await client.call_tool("run_task", {"objective": "x"})
    result = structured(raw)

    assert result["status"] == "failed"
    assert result["failure"] == {
        "code": "provider_error",
        "message": "the model provider rejected the credentials",
        "limit": None,
    }
    text = json.dumps(result) + "".join(
        block.text for block in raw.content if isinstance(block, TextContent)
    )
    assert SECRET not in text
    assert "Traceback" not in text


@pytest.mark.anyio
async def test_budget_exhaustion(workspace: Path) -> None:
    script: list[FakeStep] = [
        tool_call_response(call(f"c{i}", "list_files", path=f"x{i}")) for i in range(10)
    ]
    agent = setup(FakeProvider(script), max_consecutive_failures=50)
    async with Client(create_server(app_config(workspace), agent=agent)) as client:
        result = structured(await client.call_tool("run_task", {"objective": "x", "max_steps": 3}))

    assert result["status"] == "budget_exhausted"
    assert result["failure"]["limit"] == "max_steps"
    assert result["steps_used"] == 3


@pytest.mark.anyio
async def test_unknown_tool_call(workspace: Path) -> None:
    provider = FakeProvider(
        [tool_call_response(call("c1", "delete_everything")), text_response("Could not.")]
    )
    async with Client(create_server(app_config(workspace), agent=setup(provider))) as client:
        result = structured(await client.call_tool("run_task", {"objective": "x"}))

    assert result["executed"][0]["status"] == "rejected"
    assert "unknown tool" in result["executed"][0]["error"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"objective": ""},
        {"objective": "   "},
        {"objective": "x" * 20_001},
        {"objective": "x", "max_steps": 0},
        {"objective": "x", "max_steps": 21},
        {"objective": "x", "max_tool_calls": 51},
    ],
)
async def test_invalid_input_is_refused(workspace: Path, arguments: dict[str, Any]) -> None:
    provider = FakeProvider([text_response("never")])
    async with Client(create_server(app_config(workspace), agent=setup(provider))) as client:
        result = await client.call_tool("run_task", arguments)
    assert result.is_error
    assert provider.requests == ()


@pytest.mark.anyio
async def test_progress_is_reported(workspace: Path) -> None:
    provider = FakeProvider([tool_call_response(call("c1", "list_files")), text_response("done")])
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        seen.append((progress, total, message))

    async with Client(create_server(app_config(workspace), agent=setup(provider))) as client:
        await client.call_tool("run_task", {"objective": "x"}, progress_callback=on_progress)

    assert (1, 20, "waiting for the model") in seen
    assert (1, 20, "running list_files") in seen


class SlowProvider(Provider):
    @property
    def name(self) -> str:
        return "slow"

    async def _complete(self, request: ProviderRequest) -> ProviderResponse:
        await anyio.sleep(30)
        return text_response("late")


@pytest.mark.anyio
async def test_one_task_at_a_time(workspace: Path) -> None:
    server = create_server(app_config(workspace))
    register_run_task(server, setup(SlowProvider()), build_registry(app_config(workspace)))
    second: CallToolResult | None = None

    async with Client(server) as client, anyio.create_task_group() as group:

        async def first() -> None:
            with anyio.move_on_after(1):
                await client.call_tool("run_task", {"objective": "slow"})

        group.start_soon(first)
        await anyio.sleep(0.2)
        second = await client.call_tool("run_task", {"objective": "again"})

    assert second is not None
    assert second.is_error
    assert "another task is running" in str(second.content)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_client_cancellation_cancels_the_task(workspace: Path, mode: str) -> None:
    server = create_server(app_config(workspace))
    orchestrator = register_run_task(
        server, setup(SlowProvider()), build_registry(app_config(workspace))
    )

    async with Client(server, mode=mode) as client:
        with anyio.move_on_after(0.3):
            await client.call_tool("run_task", {"objective": "slow"})
        # The server notices the cancellation; give it a moment to record it.
        with anyio.fail_after(5):
            while not orchestrator.history.recent():
                await anyio.sleep(0.02)

    (summary,) = orchestrator.history.recent()
    assert summary.status is TaskStatus.CANCELLED
    assert summary.outcome == "cancelled"


# Approval by elicitation


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_approval_granted(workspace: Path, mode: str) -> None:
    elicit, asked = answering(APPROVE)
    server = create_server(
        app_config(workspace, write=True), agent=setup(FakeProvider(list(WRITE_SCRIPT)))
    )
    async with Client(server, mode=mode, elicitation_callback=elicit) as client:
        result = structured(await client.call_tool("run_task", {"objective": "Create it."}))

    assert (workspace / "docs" / "new.txt").read_bytes() == b"made by agent\n"
    assert result["outcome"] == "completed"
    assert result["approvals"] == [
        {
            "tool": "write_file",
            "operation": "Create new file 'docs/new.txt'",
            "decision": "approved",
        }
    ]
    assert result["verification"][0]["detail"].endswith("content matches")
    (question,) = asked
    assert isinstance(question, ElicitRequestFormParams)
    assert "Create new file 'docs/new.txt'" in question.message
    assert "made by agent" in question.message
    assert "Consequences:" in question.message
    schema = question.requested_schema
    assert schema["required"] == ["decision"]
    assert schema["properties"]["decision"]["enum"] == ["approve", "deny"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(
    ("answer", "decision"),
    [
        (DENY, "denied"),
        (ElicitResult(action="decline"), "denied"),
        (ElicitResult(action="cancel"), "cancelled"),
        (ElicitResult(action="accept", content={"decision": "approved"}), "invalid"),
        (ElicitResult(action="accept", content={"decision": "approve", "x": 1}), "invalid"),
        (ElicitResult(action="accept", content={}), "invalid"),
    ],
    ids=["deny", "decline", "cancel", "wrong-value", "extra-field", "empty"],
)
async def test_anything_but_approval_denies(
    workspace: Path, mode: str, answer: ElicitResult, decision: str
) -> None:
    elicit, asked = answering(answer)
    server = create_server(
        app_config(workspace, write=True), agent=setup(FakeProvider(list(WRITE_SCRIPT)))
    )
    async with Client(server, mode=mode, elicitation_callback=elicit) as client:
        raw = await client.call_tool("run_task", {"objective": "Create it."})

    result = structured(raw)
    assert len(asked) == 1
    assert not (workspace / "docs" / "new.txt").exists()
    assert result["approvals"][0]["decision"] == decision
    assert result["executed"][0]["status"] == "not_approved"
    assert result["outcome"] == "partially_completed"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_no_elicitation_capability_fails_closed(workspace: Path, mode: str) -> None:
    server = create_server(
        app_config(workspace, write=True), agent=setup(FakeProvider(list(WRITE_SCRIPT)))
    )
    async with Client(server, mode=mode) as client:
        result = structured(await client.call_tool("run_task", {"objective": "Create it."}))

    assert not (workspace / "docs" / "new.txt").exists()
    assert result["approvals"][0]["decision"] == "unavailable"
    assert result["outcome"] == "partially_completed"


@pytest.mark.anyio
async def test_approval_timeout(workspace: Path) -> None:
    async def never(context: Any, params: ElicitRequestParams) -> ElicitResult:
        await anyio.sleep(30)
        return APPROVE

    server = create_server(
        app_config(workspace, write=True),
        agent=setup(FakeProvider(list(WRITE_SCRIPT)), approval_timeout_seconds=0.2),
    )
    async with Client(server, mode="legacy", elicitation_callback=never) as client:
        with anyio.fail_after(10):
            result = structured(await client.call_tool("run_task", {"objective": "Create it."}))

    assert not (workspace / "docs" / "new.txt").exists()
    assert result["approvals"][0]["decision"] == "timed_out"


@pytest.mark.anyio
async def test_forged_request_state_is_refused(workspace: Path) -> None:
    server = create_server(
        app_config(workspace, write=True), agent=setup(FakeProvider(list(WRITE_SCRIPT)))
    )
    async with Client(server) as client:
        forged = json.dumps({"task_id": "task_x", "nonce": "y"})
        with pytest.raises(MCPError, match="Invalid or expired requestState"):
            await client.call_tool(
                "run_task",
                {"objective": "x"},
                request_state=forged,
                input_responses={"approval": APPROVE},
            )
    assert not (workspace / "docs" / "new.txt").exists()


@pytest.mark.anyio
async def test_the_calling_model_cannot_answer_its_own_approval(workspace: Path) -> None:
    # A client that sends answers without being asked, on the first call, gets nothing:
    # without a request_state minted by the server there is no task to resume.
    server = create_server(
        app_config(workspace, write=True), agent=setup(FakeProvider(list(WRITE_SCRIPT)))
    )
    async with Client(server) as client:
        raw = await client.call_tool(
            "run_task", {"objective": "Create it."}, input_responses={"approval": APPROVE}
        )

    result = structured(raw)
    assert not (workspace / "docs" / "new.txt").exists()
    assert result["approvals"][0]["decision"] == "unavailable"


# The entry point


def agent_environ(workspace: Path, **extra: str) -> dict[str, str]:
    return {
        ENV_WORKSPACE: str(workspace),
        ENV_ENABLE_AGENT_TOOLS: "true",
        ENV_AGENT_PROVIDER: "anthropic",
        ENV_AGENT_MODEL: "claude-test",
        ENV_ANTHROPIC_API_KEY: SECRET,
        **extra,
    }


def run_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], environ: dict[str, str]
) -> tuple[int | str | None, str]:
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as excinfo:
        main()
    captured = capsys.readouterr()
    assert captured.out == ""
    return excinfo.value.code, captured.err


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({ENV_ANTHROPIC_API_KEY: ""}, ENV_ANTHROPIC_API_KEY),
        ({ENV_AGENT_PROVIDER: "mistral"}, "unsupported provider"),
        ({ENV_AGENT_MODEL: ""}, ENV_AGENT_MODEL),
        ({ENV_ENABLE_AGENT_TOOLS: "yes"}, ENV_ENABLE_AGENT_TOOLS),
    ],
    ids=["missing-key", "unsupported-provider", "missing-model", "bad-flag"],
)
def test_main_refuses_invalid_agent_configuration(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, str],
    expected: str,
) -> None:
    code, err = run_main(monkeypatch, capsys, agent_environ(workspace, **change))
    assert code == 2
    assert expected in err
    assert SECRET not in err


def test_main_reports_a_missing_sdk(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_import = builtins.__import__

    def without_anthropic(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.partition(".")[0] == "anthropic":
            raise ImportError("No module named 'anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_anthropic)

    code, err = run_main(monkeypatch, capsys, agent_environ(workspace))

    assert code == 2
    assert "pip install 'universal-ai-tools[anthropic]'" in err
    assert SECRET not in err


def test_main_reports_a_provider_initialization_failure(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("anthropic")
    import anthropic

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"cannot start {SECRET}")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", broken)

    code, err = run_main(monkeypatch, capsys, agent_environ(workspace))

    assert code == 2
    assert "could not be initialized" in err
    assert SECRET not in err


def test_main_ignores_agent_settings_when_disabled(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from universal_ai_tools import server as server_module

    built: list[object] = []

    class Stop(Exception):
        pass

    def fake_create_server(config: AppConfig, *, agent: AgentSetup | None = None) -> Any:
        built.append(agent)
        raise Stop()

    monkeypatch.setattr(server_module, "create_server", fake_create_server)
    environ = agent_environ(
        workspace, **{ENV_ENABLE_AGENT_TOOLS: "false", ENV_AGENT_PROVIDER: "mistral"}
    )
    for name, value in environ.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(Stop):
        main()
    assert built == [None]


@pytest.mark.anyio
async def test_real_process_with_the_agent_enabled(workspace: Path) -> None:
    pytest.importorskip("anthropic")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "universal_ai_tools.server"],
        env=agent_environ(workspace),
    )
    async with Client(params, read_timeout_seconds=30) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        result = await client.call_tool("run_task", {"objective": ""})

    assert names == [*DEFAULT_TOOLS, "run_task"]
    assert result.is_error


def test_logs_never_carry_the_key(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from universal_ai_tools import server as server_module

    pytest.importorskip("anthropic")
    caplog.set_level(logging.DEBUG)

    def stop(*_args: object, **_kwargs: object) -> Any:
        raise SystemExit(0)

    monkeypatch.setattr(server_module, "create_server", stop)
    monkeypatch.setattr(server_module, "_configure_logging", lambda _level: None)
    for name, value in agent_environ(workspace).items():
        monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit):
        main()
    assert any("Agent tools are ENABLED" in r.getMessage() for r in caplog.records)
    assert all(SECRET not in r.getMessage() for r in caplog.records)

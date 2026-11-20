"""Whether a tool call may run, and how a human approves the ones that need it.

``decide`` is the whole permission policy. It looks at two things only, both fixed when
the server starts: whether the tool is in the registry, and the risk the registry gives
it. Nothing the model writes, and nothing a tool returns, is an input to it.

* A tool not in the registry: ``DENY``. This covers invented names and ``write_file`` when
  write tools are off (it is then not in the registry).
* ``ToolRisk.READ_ONLY``: ``ALLOW``. The workspace policy still applies to the call itself.
* ``ToolRisk.CREATE`` (``write_file``): ``NEEDS_APPROVAL``, every time. There is no setting
  that makes it automatic.
* Any other risk, including one added later and not handled here: ``DENY``.

An ``ApprovalChannel`` asks a human. The only answer that lets a call run is
``ApprovalDecision.APPROVED``. Every other answer (denied, no channel, timed out, cancelled,
invalid) means the call does not run. ``UnavailableApprovalChannel`` is the channel when no
human can be asked: it answers ``UNAVAILABLE`` without asking anyone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from universal_ai_tools.tools import ToolRegistry, ToolRisk, ToolSpec


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    """No human-facing channel exists, or the client does not support one."""
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INVALID = "invalid"
    """The channel answered with something that is not a clear approval or denial."""


def decide(registry: ToolRegistry, name: str) -> tuple[Decision, ToolSpec[Any, Any] | None]:
    """The policy decision for a call of the tool registered as ``name``."""
    if not isinstance(name, str) or name not in registry:
        return Decision.DENY, None
    spec = registry.get(name)
    if spec.risk is ToolRisk.READ_ONLY:
        return Decision.ALLOW, spec
    if spec.risk is ToolRisk.CREATE:
        return Decision.NEEDS_APPROVAL, spec
    return Decision.DENY, spec


@dataclass(frozen=True)
class ApprovalRequest:
    """What a human is asked to approve. Every text field is escaped and bounded."""

    task_id: str
    tool_call_id: str
    tool: str
    risk: str
    operation: str
    """One line: what will happen, with the path."""
    consequences: str
    """What approving means, in fixed words chosen by the tool's risk."""
    details: tuple[tuple[str, str], ...]
    """Labelled facts for the human: target path, size, a preview of the content."""


class ApprovalPending(Exception):
    """The question has been handed to the human, but the answer will arrive later.

    Raised by a channel that cannot wait within the current call. The orchestrator then
    suspends the task, and continues it when ``Orchestrator.resume`` brings the answer.
    """


class ApprovalChannel(Protocol):
    """A way to ask a human. Must never answer ``APPROVED`` on anyone else's behalf."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """The human's answer.

        Raises:
            ApprovalPending: If the answer can only arrive in a later call.
        """
        ...


class DeferredApprovalChannel:
    """Hands every question to a later call: the task is suspended until the answer comes."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        raise ApprovalPending()


class UnavailableApprovalChannel:
    """No human can be asked, so nothing that needs approval runs."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.UNAVAILABLE


CONSEQUENCES = {
    ToolRisk.CREATE: (
        "A new file will be created in the workspace with the content shown. Nothing that "
        "exists is overwritten, renamed, or deleted. The file stays after the task ends, and "
        "the content was written by the model, so review it before you use or run it."
    ),
}

"""The gate: the one path every MCP tool call takes from the client to its handler.

For each call, in order:

1. The arguments are validated strictly against the tool's input model, and reduced to one
   canonical JSON form (``_canonical``). Its SHA-256 digest identifies the call. From here
   on, nothing reads the arguments the caller passed: the description shown to the human
   and the arguments the handler receives are both rebuilt from that canonical form, so
   what was approved is exactly what runs, and changing the caller's objects afterwards
   changes nothing.
2. The permission decision, which depends only on the configuration and on the tool's
   risk and capability, all fixed at startup. Nothing the model writes, and nothing a tool
   returns, is an input to it:

   * environment mode: no server-side approval. A client can reach environment mode only
     read-only (``settings.ENVIRONMENT_WRITES_REFUSED``);
   * file mode, ``READ_ONLY``: runs;
   * file mode, anything else: needs a human's approval, under the capability's policy:
     ``every_call``, or ``once_per_session`` (one approval allows that capability for at
     most ``[approval] session_grant_minutes``). A tool that executes code (``EXECUTE``) is
     always ``every_call``, whatever its capability's policy says, and so is a tool marked
     ``always_ask`` (``request_input_grant``);
   * an input tool call covered by an active input grant (``desktop/grants.py``), which the
     user approved for that program and a limited time, runs without a new question; its
     records carry ``"channel": "grant"`` and the grant's id.

3. Approval, when needed, through the channel ``universal_ai_tools.approval`` describes.
   The complete request is rendered for that channel's dialog; if it does not fit, the call
   is refused (``too_large``) rather than shown in part. Only an explicit approval lets the
   call run. Approval questions are asked one at a time.
4. A ``started`` record in the audit log, written durably. If it cannot be written, the call
   is refused.
5. The handler, through ``ToolRegistry.ainvoke`` on a new copy of the canonical form,
   which the registry validates strictly again immediately before dispatch.
6. A ``finished`` record: succeeded, failed (with the safe error message and an error class),
   or cancelled. A result is returned only once that record is written: if it cannot be, a
   read returns an error instead of the data, and a change that has already run (after its
   durable ``started`` record) is reported as run but unrecorded. A deferred approval
   question whose record cannot be written is not sent.

MCP 2026-07-28 does not let a server ask the client anything in the middle of a call. Then,
when the client's dialog is the channel, step 3 returns an ``InputRequiredResult`` carrying
the question; the client asks the human and calls again with the answer. The pending
question is remembered here, keyed by a random nonce, bound to the tool and the digest of
its canonical arguments, and it expires after the approval timeout. An answer that does not
match a pending question approves nothing, and an approved retry runs the canonical
arguments stored with the question, not those of the retry.

The audit log (file mode only) gets a record for every call, read-only ones included:
the session and call identifiers, the client's request id, the tool, its operation and
risk, the target (``ActionDescription.audit``: paths, never contents), the digest of the
arguments, the permission decision, the approval decision and channel, the status, and an
error class. Calls refused before they could run (unknown tool, invalid arguments, not
approved, too large to show) are recorded as ``refused``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
from mcp.types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult
from pydantic import BaseModel, ValidationError

from universal_ai_tools.approval import (
    ApprovalChannel,
    ApprovalDecision,
    ApprovalForm,
    ApprovalRequest,
    ApprovalTooLargeError,
    DesktopDialogChannel,
    DisplayLimits,
    ElicitationChannel,
    ShowDialog,
    UnavailableChannel,
    client_supports_form_elicitation,
    decision_from_responses,
    desktop_dialog_available,
    render_approval,
)
from universal_ai_tools.audit import AuditError, AuditLog
from universal_ai_tools.policy import escape_line
from universal_ai_tools.settings import ApprovalPolicy, ServiceConfig
from universal_ai_tools.tools import (
    ActionDescription,
    ToolCallError,
    ToolInputError,
    ToolRegistry,
    ToolRisk,
    ToolSpec,
    UnknownToolError,
)
from universal_ai_tools.workspace import (
    FileTooLargeError,
    HardLinkError,
    InvalidPathError,
    LinkRefusedError,
    OutsideWorkspaceError,
    PathNotFoundError,
    PolicyRefusedError,
    ProtectedPathError,
    WorkspaceError,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import Context

logger = logging.getLogger(__name__)

APPROVAL_KEY = "approval"
MAX_PENDING_APPROVALS = 16

_DECISION_WORDS = {
    ApprovalDecision.DENIED: "you denied it",
    ApprovalDecision.UNAVAILABLE: "no approval dialog could be shown (the client does not "
    "support MCP elicitation and no desktop dialog is available)",
    ApprovalDecision.TIMED_OUT: "no answer came in time",
    ApprovalDecision.CANCELLED: "the approval dialog was cancelled or dismissed",
    ApprovalDecision.INVALID: "the answer was not a clear approval",
    ApprovalDecision.TOO_LARGE: "it is too large to show in full for approval",
}


class NotApprovedError(ToolCallError):
    """The human did not approve the call, so it did not run."""


class ArgumentsChangedError(ToolCallError):
    """The arguments about to run are not the ones that were approved."""


def error_class(exc: BaseException) -> str:
    """A fixed word for what went wrong, for the audit log."""
    classes: tuple[tuple[type[BaseException], str], ...] = (
        (NotApprovedError, "not_approved"),
        (ArgumentsChangedError, "arguments_changed"),
        (UnknownToolError, "unknown_tool"),
        (ToolInputError, "invalid_arguments"),
        (ProtectedPathError, "protected_path"),
        (PolicyRefusedError, "policy_refused"),
        (OutsideWorkspaceError, "outside_workspace"),
        (InvalidPathError, "invalid_path"),
        (LinkRefusedError, "link_refused"),
        (HardLinkError, "hard_link_refused"),
        (FileTooLargeError, "too_large"),
        (PathNotFoundError, "not_found"),
        (WorkspaceError, "filesystem_error"),
        (ToolCallError, "refused_by_tool"),
    )
    for kind, name in classes:
        if isinstance(exc, kind):
            return name
    return "internal_error"


@dataclass(frozen=True)
class _Pending:
    tool: str
    digest: str
    canonical: str
    expires: float
    request: ApprovalRequest


@dataclass(frozen=True)
class _Outcome:
    decision: ApprovalDecision
    channel: str
    canonical: str
    """The arguments the decision is about, in canonical form."""
    reason: str = ""


class Gate:
    """Decides, asks, records, and runs. One per server process."""

    def __init__(
        self,
        service: ServiceConfig,
        registry: ToolRegistry,
        *,
        audit: AuditLog | None = None,
        show_dialog: ShowDialog | None = None,
        dialog_available: Callable[[], bool] | None = None,
        dialog_limits: DisplayLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._registry = registry
        self._audit = audit or AuditLog(None)
        self._show_dialog = show_dialog
        self._dialog_available = dialog_available or desktop_dialog_available
        self._dialog_limits = dialog_limits
        self._clock = clock
        self.session_id = secrets.token_hex(6)
        self._grants: dict[str, float] = {}
        self._pending: dict[str, _Pending] = {}
        self._approval_lock = anyio.Lock()

    @property
    def audit(self) -> AuditLog:
        return self._audit

    def policy_for(self, spec: ToolSpec[Any, Any]) -> ApprovalPolicy | None:
        """The approval policy for a tool, or None when it runs without approval."""
        if not self._service.file_mode or spec.risk is ToolRisk.READ_ONLY:
            return None
        if spec.risk is ToolRisk.EXECUTE or spec.always_ask:
            # Code that runs, or a request for a grant: a fresh approval for every call,
            # whatever the configuration.
            return "every_call"
        policies: dict[str, ApprovalPolicy] = {
            "filesystem": self._service.filesystem.approval,
            "screen": self._service.screen.approval,
            # Saving screenshots as files: the screen's policy, but approved separately, so
            # an approval to look at the screen never also allows writing files.
            "screenshot-files": self._service.screen.approval,
            "input": self._service.input.approval,
        }
        # The terminal, and any capability not listed, is asked about every time.
        return policies.get(spec.capability, "every_call")

    async def call(
        self, name: str, arguments: Mapping[str, object], ctx: Context | None = None
    ) -> BaseModel | InputRequiredResult:
        """Run one call of the tool registered as ``name``.

        Raises:
            ToolCallError: If the tool is unknown, the arguments are invalid, the call was not
                approved, or it cannot be audited.
            WorkspaceError: Raised by a filesystem handler for a refused or failed request.
        """
        record: dict[str, object] = {
            "session": self.session_id,
            "call": secrets.token_hex(6),
            "request": _request_id(ctx),
            "client": _client_name(ctx),
            "protocol": ctx.protocol_version if ctx is not None else None,
            "client_elicitation": ctx is not None and client_supports_form_elicitation(ctx),
            "tool": escape_line(name)[:64] if isinstance(name, str) else type(name).__name__,
        }
        try:
            spec = self._registry.get(name)
            record["operation"] = spec.operation
            record["risk"] = spec.risk.value
            validated = spec.validate(arguments)
            canonical = _canonical(validated)
            record["arguments_sha256"] = _digest(spec.name, canonical)
            description = _describe(spec, canonical)
            record["target"] = dict(description.audit) if description is not None else {}
        except ToolCallError as exc:
            self._refused(record, exc)
            raise
        started = self._clock()

        policy = self.policy_for(spec)
        record["permission"] = "approval_required" if policy is not None else "allowed"
        grant_id = self._grant_for(spec, validated) if policy is not None else None
        if grant_id is not None:
            # The user approved a grant for this program and tool (``desktop/grants.py``).
            record["policy"] = policy
            record["approval"] = {"decision": "approved", "channel": "grant", "grant": grant_id}
        elif policy is not None:
            record["policy"] = policy
            if description is None:  # ToolSpec requires it for every risk but READ_ONLY
                raise ToolCallError(f"'{spec.name}' cannot be described for approval")
            request = ApprovalRequest(
                tool=spec.name,
                risk=spec.risk.value,
                operation=description.operation,
                consequences=description.consequences,
                details=description.details,
                scope="session" if policy == "once_per_session" else "call",
                capability=spec.capability,
                session_minutes=self._service.approval.session_grant_minutes,
                client=_client_name(ctx) or "the MCP client",
            )
            try:
                outcome = await self._approve(spec, policy, request, canonical, ctx)
            except ToolCallError as exc:
                self._refused(record, exc)
                raise
            if isinstance(outcome, InputRequiredResult):
                if not self._audit.try_record(
                    "approval_requested", {**record, "channel": "client", "status": "waiting"}
                ):
                    raise ToolCallError(
                        f"'{spec.name}' did not run because the audit log cannot be written"
                    )
                return outcome
            record["approval"] = {"decision": outcome.decision.value, "channel": outcome.channel}
            if outcome.decision is not ApprovalDecision.APPROVED:
                words = _DECISION_WORDS.get(outcome.decision, outcome.decision.value)
                refusal = NotApprovedError(
                    f"'{spec.name}' did not run because it was not approved: {words}"
                    + (f" ({outcome.reason})" if outcome.reason else "")
                )
                self._refused(record, refusal)
                raise refusal
            canonical = outcome.canonical

        # What runs is a new copy of the canonical form that was described and approved,
        # never the caller's objects, which may have changed meanwhile. The registry
        # validates it strictly once more before dispatch.
        arguments_to_run = json.loads(canonical)

        consequential = self._service.file_mode and spec.risk is not ToolRisk.READ_ONLY
        if consequential and self._audit.enabled:
            try:
                self._audit.record("started", {**record, "status": "running"}, durable=True)
            except AuditError as exc:
                raise ToolCallError(
                    f"'{spec.name}' did not run because the audit log cannot be written ({exc})"
                ) from None

        try:
            result = await self._registry.ainvoke(spec.name, arguments_to_run)
        except anyio.get_cancelled_exc_class():
            self._finished(record, started, "cancelled")
            raise
        except Exception as exc:
            # The call has already failed, so a record that cannot be written changes nothing.
            self._finished(record, started, "failed", exc)
            raise
        if self._finished(record, started, "succeeded"):
            return result
        # Fail closed: nothing is returned that the audit log does not show.
        if consequential:
            raise ToolCallError(
                f"'{spec.name}' ran (its 'started' record is in the audit log), but its result "
                "could not be recorded, so it is withheld; the audit log cannot be written"
            )
        raise ToolCallError(
            f"'{spec.name}' result withheld because the audit log cannot be written"
        )

    @staticmethod
    def _grant_for(spec: ToolSpec[Any, Any], validated: BaseModel) -> str | None:
        """The active grant covering this call, if any. Never for a tool that always asks."""
        if spec.always_ask or spec.grant is None or spec.risk is ToolRisk.EXECUTE:
            return None
        try:
            return spec.grant(validated)
        except Exception:
            logger.exception("checking the grants for '%s' failed; asking instead", spec.name)
            return None

    def _finished(
        self,
        record: Mapping[str, object],
        started: float,
        status: str,
        exc: BaseException | None = None,
    ) -> bool:
        """Append the ``finished`` record. False if it could not be written."""
        finished = {
            **record,
            "status": status,
            "duration_ms": round((self._clock() - started) * 1000),
        }
        if exc is not None:
            finished["error"] = _safe_error(exc)
            finished["error_class"] = error_class(exc)
        return self._audit.try_record("finished", finished)

    def _refused(self, record: Mapping[str, object], exc: BaseException) -> None:
        self._audit.try_record(
            "refused",
            {
                **record,
                "status": "refused",
                "error": _safe_error(exc),
                "error_class": error_class(exc),
            },
        )

    async def _approve(
        self,
        spec: ToolSpec[Any, Any],
        policy: ApprovalPolicy,
        request: ApprovalRequest,
        canonical: str,
        ctx: Context | None,
    ) -> _Outcome | InputRequiredResult:
        now = self._clock()
        if policy == "once_per_session" and self._grants.get(spec.capability, 0) > now:
            return _Outcome(ApprovalDecision.APPROVED, "session_grant", canonical)

        digest = _digest(spec.name, canonical)
        state = ctx.request_state if ctx is not None else None
        if state is not None:
            pending = self._take_pending(state, spec.name, digest)
            decision = decision_from_responses(
                ctx.input_responses if ctx is not None else None, APPROVAL_KEY
            )
            outcome = _Outcome(decision, "client", pending.canonical)
        else:
            channel = self._channel(ctx)
            if isinstance(channel, UnavailableChannel):
                return _Outcome(ApprovalDecision.UNAVAILABLE, channel.name, canonical)
            try:
                message = render_approval(request, channel.limits(), closing=channel.closing)
            except ApprovalTooLargeError as exc:
                return _Outcome(ApprovalDecision.TOO_LARGE, channel.name, canonical, str(exc))
            if isinstance(channel, ElicitationChannel) and ctx is not None and not _can_ask(ctx):
                return self._defer(spec.name, digest, canonical, request, message)
            async with self._approval_lock:
                decision = ApprovalDecision.TIMED_OUT
                with anyio.move_on_after(self._service.approval.timeout_seconds):
                    decision = await channel.request_approval(request, message)
            outcome = _Outcome(decision, channel.name, canonical)
        if outcome.decision is ApprovalDecision.APPROVED and policy == "once_per_session":
            self._grants[spec.capability] = (
                self._clock() + self._service.approval.session_grant_minutes * 60
            )
        return outcome

    def _channel(self, ctx: Context | None) -> ApprovalChannel:
        mode = self._service.approval.channel
        if mode in ("auto", "client") and ctx is not None and client_supports_form_elicitation(ctx):
            return ElicitationChannel(ctx)
        if mode in ("auto", "desktop_dialog") and self._dialog_available():
            return DesktopDialogChannel(
                self._show_dialog,
                timeout_seconds=self._service.approval.timeout_seconds,
                limits=self._dialog_limits,
            )
        return UnavailableChannel()

    def _defer(
        self, tool: str, digest: str, canonical: str, request: ApprovalRequest, message: str
    ) -> InputRequiredResult:
        from mcp.server.elicitation import render_elicitation_schema

        now = self._clock()
        self._pending = {k: v for k, v in self._pending.items() if v.expires > now}
        if len(self._pending) >= MAX_PENDING_APPROVALS:
            raise ToolCallError("too many approval requests are waiting; answer them first")
        nonce = secrets.token_urlsafe(16)
        self._pending[nonce] = _Pending(
            tool, digest, canonical, now + self._service.approval.timeout_seconds, request
        )
        return InputRequiredResult(
            input_requests={
                APPROVAL_KEY: ElicitRequest(
                    params=ElicitRequestFormParams(
                        message=message,
                        requested_schema=render_elicitation_schema(ApprovalForm),
                    )
                )
            },
            request_state=json.dumps({"approval": nonce}),
        )

    def _take_pending(self, state: str, tool: str, digest: str) -> _Pending:
        try:
            nonce = json.loads(state).get("approval")
        except (ValueError, AttributeError):
            nonce = None
        pending = self._pending.pop(nonce, None) if isinstance(nonce, str) else None
        if (
            pending is None
            or pending.expires <= self._clock()
            or pending.tool != tool
            or pending.digest != digest
        ):
            raise ToolCallError(
                "this approval is no longer pending (it was answered, expired, or was for a "
                "different call); call the tool again"
            )
        return pending


def _can_ask(ctx: Context) -> bool:
    try:
        return bool(ctx.session.can_send_request)
    except (AttributeError, ValueError):
        return False


def _canonical(validated: BaseModel) -> str:
    """The one JSON text for these arguments: sorted keys, ASCII only, no spaces."""
    return json.dumps(validated.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _model(spec: ToolSpec[Any, Any], canonical: str) -> BaseModel:
    """A new input model, validated strictly from the canonical form."""
    try:
        model: BaseModel = spec.input_model.model_validate(json.loads(canonical), strict=True)
    except (ValueError, ValidationError):
        raise ArgumentsChangedError(
            f"'{spec.name}' did not run because its arguments cannot be reproduced exactly"
        ) from None
    return model


def _describe(spec: ToolSpec[Any, Any], canonical: str) -> ActionDescription | None:
    if spec.describe is None:
        return None
    return spec.describe(_model(spec, canonical))


def _digest(tool: str, canonical: str) -> str:
    return hashlib.sha256(f"{tool}\n{canonical}".encode()).hexdigest()


def _safe_error(exc: BaseException) -> str:
    # Only messages written to be shown are recorded; others could hold a full path.
    if isinstance(exc, ToolCallError | WorkspaceError):
        return escape_line(str(exc))[:500]
    return type(exc).__name__


def _request_id(ctx: Context | None) -> str | None:
    if ctx is None:
        return None
    try:
        value = ctx.request_id
    except (AttributeError, ValueError, LookupError):
        return None
    return escape_line(str(value))[:80] if value is not None else None


def _client_name(ctx: Context | None) -> str:
    if ctx is None:
        return ""
    try:
        params = ctx.session.client_params
    except (AttributeError, ValueError):
        return ""
    info = getattr(params, "client_info", None)
    if info is None:
        return ""
    name = escape_line(str(getattr(info, "name", "")))[:80]
    version = escape_line(str(getattr(info, "version", "")))[:40]
    return f"{name} {version}".strip()


__all__ = ["APPROVAL_KEY", "Gate", "NotApprovedError", "error_class"]

"""ToolSpec invariants for the risks added with the corrected architecture."""

from __future__ import annotations

import pytest
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from universal_ai_tools.tools import STRICT_INPUT, ActionDescription, ToolRisk, ToolSpec


class _In(BaseModel):
    model_config = STRICT_INPUT


class _Out(BaseModel):
    ok: bool = True


def _spec(risk: ToolRisk, *, read_only: bool, destructive: bool, describe: bool = True) -> object:
    return ToolSpec(
        name="probe",
        title="Probe",
        description="probe",
        input_model=_In,
        output_model=_Out,
        handler=lambda _arguments: _Out(),
        risk=risk,
        annotations=ToolAnnotations(
            title="Probe", read_only_hint=read_only, destructive_hint=destructive
        ),
        describe=(lambda _arguments: ActionDescription("probe", "nothing")) if describe else None,
    )


@pytest.mark.parametrize(
    ("risk", "read_only", "destructive"),
    [
        (ToolRisk.READ_ONLY, True, False),
        (ToolRisk.OBSERVE, True, False),
        (ToolRisk.CREATE, False, False),
        (ToolRisk.MODIFY, False, True),
        (ToolRisk.EXECUTE, False, True),
        (ToolRisk.INPUT, False, True),
    ],
)
def test_each_risk_has_exactly_one_valid_annotation(
    risk: ToolRisk, read_only: bool, destructive: bool
) -> None:
    _spec(risk, read_only=read_only, destructive=destructive)
    with pytest.raises(ValueError, match="contradicts risk"):
        _spec(risk, read_only=not read_only, destructive=destructive)
    with pytest.raises(ValueError, match="contradicts risk"):
        _spec(risk, read_only=read_only, destructive=not destructive)


@pytest.mark.parametrize(
    "risk", [ToolRisk.OBSERVE, ToolRisk.CREATE, ToolRisk.MODIFY, ToolRisk.EXECUTE, ToolRisk.INPUT]
)
def test_every_risk_but_read_only_must_describe_itself_for_approval(risk: ToolRisk) -> None:
    read_only = risk is ToolRisk.OBSERVE
    destructive = risk in (ToolRisk.MODIFY, ToolRisk.EXECUTE, ToolRisk.INPUT)
    with pytest.raises(ValueError, match="must describe it"):
        _spec(risk, read_only=read_only, destructive=destructive, describe=False)

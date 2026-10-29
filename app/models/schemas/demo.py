"""Bounded, synthetic-only browser demonstration inputs."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9._:-]{1,64}$')]


class DemoInput(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class StartSession(DemoInput):
    request_id: UUID
    route_id: Identifier
    retailer_id: Identifier


class SessionAction(DemoInput):
    request_id: UUID
    expected_version: int = Field(strict=True, ge=0, le=40)
    action: Literal['input', 'back', 'cancel']
    digit: Annotated[str, StringConstraints(pattern=r'^[0-9]$')] | None = None


class AnalyzeSelection(DemoInput):
    request_id: UUID
    observation_ids: list[Identifier] = Field(min_length=1, max_length=8)
    mode: Literal['mock', 'simulated']


class ReviewDecision(DemoInput):
    request_id: UUID
    analysis_id: Identifier
    strategy_index: int = Field(strict=True, ge=0, le=2)
    decision: Literal['reviewed', 'planning', 'rejected']
    demo_only: Literal[True]

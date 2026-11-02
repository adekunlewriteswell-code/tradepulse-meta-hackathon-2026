"""Explicit human actions and bounded integer-kobo simulation inputs."""
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from app.domain.timestamps import normalize_utc_timestamp
from app.models.schemas.demo import DemoInput, Identifier
from uuid import UUID


class WorkflowAction(DemoInput):
    request_id: UUID
    demo_only: Literal[True]

    @field_validator('demo_only', 'human_approved', 'simulated_purchase', check_fields=False, mode='before')
    @classmethod
    def explicit_acknowledgment(cls, value):
        if value is not True:
            raise ValueError('Explicit demonstration acknowledgment is required')
        return value


class DraftMission(WorkflowAction):
    analysis_id: Identifier
    strategy_index: int = Field(strict=True, ge=0, le=2)
    observation_ids: list[Identifier] = Field(min_length=1, max_length=8)
    product: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    target: int = Field(strict=True, ge=1, le=8)
    minimum_quantity: int = Field(strict=True, ge=1, le=100000)
    starts_at: str
    ends_at: str
    incentive_bps: int = Field(strict=True, ge=0, le=2000)
    margin_guardrail_bps: int = Field(strict=True, ge=0, le=10000)
    reward_cap_minor: int = Field(strict=True, ge=0, le=1000000000)
    minimum_contribution_minor: int = Field(strict=True, ge=1, le=1000000000)

    @field_validator('starts_at', 'ends_at')
    @classmethod
    def utc(cls, value):
        return normalize_utc_timestamp(value)

    @model_validator(mode='after')
    def distinct(self) -> Self:
        if len(self.observation_ids) != len(set(self.observation_ids)):
            raise ValueError('Select unique observation IDs')
        return self


class ApproveMission(WorkflowAction):
    human_approved: Literal[True]


class RecordCommitment(WorkflowAction):
    observation_id: Identifier
    quantity: int = Field(strict=True, ge=1, le=100000)


class SimulatedPurchase(WorkflowAction):
    commitment_id: Identifier
    order_ref: str = Field(pattern=r'^[A-Za-z0-9._-]{1,60}$')
    product: str = Field(min_length=1, max_length=80)
    quantity: int = Field(strict=True, ge=1, le=100000)
    unit_price_minor: int = Field(strict=True, ge=1, le=100000000)
    unit_cost_minor: int = Field(strict=True, ge=0, le=100000000)
    fulfillment_per_unit_minor: int = Field(strict=True, ge=0, le=100000000)
    other_incremental_costs_minor: int = Field(strict=True, ge=0, le=10000000000)
    purchased_at: str
    simulated_purchase: Literal[True]

    @field_validator('purchased_at')
    @classmethod
    def utc(cls, value):
        return normalize_utc_timestamp(value)


class ReleaseReward(WorkflowAction):
    human_approved: Literal[True]

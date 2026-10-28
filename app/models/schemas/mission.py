"""Mission metadata validation only; this does not authenticate a manager."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.domain.timestamps import normalize_utc_timestamp


class MissionMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid')

    starts_at: str
    ends_at: str
    status: Literal['DRAFT', 'ACTIVE', 'TARGET_MET', 'REWARD_RELEASED', 'EXPIRED'] = 'DRAFT'
    approved_by: str | None = None
    approved_at: str | None = None
    parameters_hash: str | None = None

    @field_validator('starts_at', 'ends_at', 'approved_at', mode='before')
    @classmethod
    def canonical_utc(cls, value):
        return None if value is None else normalize_utc_timestamp(value)

    @field_validator('approved_by', 'parameters_hash')
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError('Approval metadata must not be blank')
        return value

    @model_validator(mode='after')
    def valid_metadata(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError('Mission end must be later than start')
        if self.status not in {'DRAFT', 'EXPIRED'} and any(
            value is None for value in (self.approved_by, self.approved_at, self.parameters_hash)
        ):
            raise ValueError('Approval metadata is required for this mission status')
        return self

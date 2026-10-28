"""Mock-compatible two-task AI contract, including evidence validation."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationInfo, model_validator

Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class StrictSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ObservationInput(StrictSchema):
    id: Nonempty
    retailer_id: Nonempty
    menu_path: list[Nonempty] = Field(min_length=1)
    products: list[Nonempty]
    stated_barrier: Nonempty


class AnalysisRequest(StrictSchema):
    route_baseline_id: Nonempty
    observations: list[ObservationInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_observation_ids(self) -> Self:
        ids = [item.id for item in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("Observation IDs must be unique")
        return self


class Barrier(StrictSchema):
    barrier: Nonempty
    evidence: Nonempty
    confidence: Confidence


class ActivationStrategy(StrictSchema):
    strategy: Nonempty
    evidence_basis: list[Nonempty] = Field(min_length=1)
    confidence: Confidence
    preconditions: list[Nonempty]
    risks: list[Nonempty]


class AnalysisResponse(StrictSchema):
    extracted_products: list[Nonempty]
    intent_level: Literal["none", "interest", "conditional", "firm"]
    stated_barriers: list[Barrier]
    uncertainty_flags: list[Nonempty]
    pattern_summary: Nonempty
    activation_strategies: list[ActivationStrategy] = Field(min_length=1, max_length=3)
    uncertainty_statement: Nonempty

    @model_validator(mode="after")
    def references_supplied_observations(self, info: ValidationInfo) -> Self:
        # Schema-only validation is useful, but production service always supplies context.
        if info.context is not None:
            allowed = set(info.context["observation_ids"])
            cited = {item.evidence for item in self.stated_barriers}
            cited.update(ref for strategy in self.activation_strategies for ref in strategy.evidence_basis)
            unknown = cited - allowed
            if unknown:
                raise ValueError(f"Evidence references unknown observation IDs: {sorted(unknown)}")
        return self

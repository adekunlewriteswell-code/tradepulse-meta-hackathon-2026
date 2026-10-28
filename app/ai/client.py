"""AI client protocol and a deterministic, explicitly labelled test double."""

from typing import Protocol

from app.models.schemas.ai import AnalysisRequest, AnalysisResponse


class AIUnavailableError(RuntimeError):
    """Expected provider unavailability; never replace a failure with mock output."""


class AIClient(Protocol):
    model_name: str
    data_source: str

    async def extract_and_strategise(self, payload: AnalysisRequest) -> str: ...


class MockAIClient:
    model_name = "mock-deterministic-v1"
    data_source = "mock"

    async def extract_and_strategise(self, payload: AnalysisRequest) -> str:
        observations = sorted(payload.observations, key=lambda item: item.id)
        response = AnalysisResponse.model_validate({
            "extracted_products": sorted({p for item in observations for p in item.products}),
            "intent_level": "none",
            "stated_barriers": [
                {"barrier": item.stated_barrier, "evidence": item.id, "confidence": 0.5}
                for item in observations
            ],
            "uncertainty_flags": ["MOCK_OUTPUT", "Intent and commercial outcomes are not inferred"],
            "pattern_summary": "Deterministic mock: supplied barriers only; no commercial inference performed.",
            "activation_strategies": [{
                "strategy": "Mock recommendation: manager reviews the supplied barriers before proposing outreach.",
                "evidence_basis": [item.id for item in observations],
                "confidence": 0.5,
                "preconditions": ["Manager review and explicit approval"],
                "risks": ["Mock responses do not demonstrate model inference or commercial effectiveness"],
            }],
            "uncertainty_statement": "MOCK: not Meta inference or a verified commercial fact; real outcomes are unknown.",
        }, context={"observation_ids": [item.id for item in observations]})
        return response.model_dump_json()

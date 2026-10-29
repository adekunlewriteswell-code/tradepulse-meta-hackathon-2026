"""Deterministic HTTP fixture, not a model or commercial inference."""
import json

import httpx

from app.ai.client import MockAIClient
from app.models.schemas.ai import AnalysisRequest


def simulated_transport() -> httpx.MockTransport:
    async def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        supplied = json.loads(body["messages"][1]["content"])["analysis_input"]
        payload = AnalysisRequest.model_validate(supplied)
        content = json.loads(await MockAIClient().extract_and_strategise(payload))
        content["pattern_summary"] = "SIMULATED HTTP FIXTURE - supplied synthetic barriers only."
        content["uncertainty_flags"] = ["SIMULATED_TRANSPORT", "NOT_GENUINE_INFERENCE"]
        content["uncertainty_statement"] = (
            "SIMULATED META HTTP RESPONSE - not genuine inference or verified commercial outcomes."
        )
        proposals = (
            "Manager reviews a smaller synthetic trial-order option against the stated constraint.",
            "Manager reviews a fixed delivery window, subject to capacity confirmation.",
            "Manager reviews lower initial stocking quantities; affordability and sales remain unknown.",
        )
        content["activation_strategies"] = [{
            "strategy": proposal, "evidence_basis": [observation.id], "confidence": 0.5,
            "preconditions": ["Manager verifies suitability and approves any subsequent action"],
            "risks": ["Synthetic fixture does not establish real demand or commercial effectiveness"],
        } for proposal, observation in zip(proposals, payload.observations)]
        return httpx.Response(200, json={
            "model": body["model"], "choices": [{
                "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(content), "refusal": None,
                }}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 180, "total_tokens": 300},
        }, headers={"x-request-id": "synthetic-http-fixture"})
    return httpx.MockTransport(respond)


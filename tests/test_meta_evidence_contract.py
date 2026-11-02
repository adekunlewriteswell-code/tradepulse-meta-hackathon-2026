"""Reviewed synthetic derivative of the persisted ID-plus-prose failure."""
from copy import deepcopy
import json
from pathlib import Path

import httpx
from pydantic import ValidationError
import pytest

from app.ai.meta import MetaAIClient
from app.ai.prompt import INSTRUCTIONS, PROMPT_VERSION, provider_request
from app.models.schemas.ai import AnalysisRequest, AnalysisResponse
from app.services.ai_analysis import AIAnalysisService
from test_meta_adapter import meta_settings


@pytest.fixture
def redacted_saved_response():
    path = Path(__file__).parent / "fixtures/meta_evidence_prose_redacted.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    return AnalysisRequest.model_validate(fixture["analysis_input"]), fixture["response"]


def evidence_context(payload):
    return {
        "observation_ids": [v.id for v in payload.observations],
        "products": {p for v in payload.observations for p in v.products},
        "barriers": {v.id: v.stated_barrier for v in payload.observations},
    }


@pytest.mark.parametrize("count", [1, 8])
def test_request_schema_requires_exact_supplied_evidence_ids(redacted_saved_response, count):
    payload, _ = redacted_saved_response
    payload = AnalysisRequest.model_validate({
        **payload.model_dump(), "observations": [v.model_dump() for v in payload.observations[:count]],
    })
    unchanged_schema = AnalysisResponse.model_json_schema()
    body = provider_request(payload, "muse-spark-1.3", 4096)
    assert body["reasoning_effort"] == "low"
    assert body["max_completion_tokens"] == 4096
    assert body["response_format"]["json_schema"]["strict"] is True
    schema = body["response_format"]["json_schema"]["schema"]
    expected_ids = [v.id for v in payload.observations]
    barrier = schema["$defs"]["Barrier"]["properties"]["evidence"]
    strategy = schema["$defs"]["ActivationStrategy"]["properties"]["evidence_basis"]["items"]
    assert barrier["enum"] == strategy["enum"] == expected_ids
    assert all(v + " [EXPLANATION REDACTED]" not in barrier["enum"] for v in expected_ids)
    assert "with no appended explanation" in INSTRUCTIONS
    assert PROMPT_VERSION == "meta-barriers-strategies-v2"
    assert AnalysisResponse.model_json_schema() == unchanged_schema
    subsequent = provider_request(payload, "muse-spark-1.3", 4096)
    assert subsequent == body
    encoded = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode()
    assert len(encoded) <= 16384
    assert len(encoded) + 4096 <= 12000


@pytest.mark.parametrize("count", [1, 8])
async def test_saved_response_id_plus_prose_defect_is_rejected_without_repair_or_retry(
    settings, redacted_saved_response, count,
):
    payload, response = redacted_saved_response
    if count == 1:
        payload = AnalysisRequest.model_validate({
            **payload.model_dump(), "observations": [payload.observations[0].model_dump()],
        })
        response["stated_barriers"] = response["stated_barriers"][:1]
        response["extracted_products"] = payload.observations[0].products
        response["activation_strategies"] = response["activation_strategies"][:1]
        response["activation_strategies"][0]["evidence_basis"] = [response["stated_barriers"][0]["evidence"]]
    # The historical output passed its wire schema but failed the evidence contract.
    AnalysisResponse.model_validate(response)
    with pytest.raises(ValidationError) as captured:
        AnalysisResponse.model_validate(response, context=evidence_context(payload))
    errors = captured.value.errors(include_input=False, include_context=False, include_url=False)
    assert len(errors) == 1 and errors[0]["type"] == "value_error" and errors[0]["loc"] == ()
    assert "Evidence references unknown observation IDs" in errors[0]["msg"]
    raw = json.dumps(response)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "model": "muse-spark-1.3", "choices": [{
                "finish_reason": "stop", "message": {"role": "assistant", "content": raw},
            }],
        })

    client = MetaAIClient(meta_settings(
        settings, meta_max_output_tokens=4096, meta_max_calls=1, meta_max_retries=0,
    ), transport=httpx.MockTransport(handler))
    try:
        result = await AIAnalysisService(client).analyze(payload)
        assert result.output is None
        assert result.provenance.parse_status == "PARSED"
        assert result.provenance.validation_status == "EXTRACTION_INVALID"
        assert result.provenance.call_details["error_category"] == "schema_or_evidence"
        assert result.provenance.raw_response == raw
        assert result.provenance.parsed_output is None
        assert len(calls) == client.calls_reserved == 1
        assert result.provenance.call_details["attempts"] == 1
    finally:
        await client.aclose()


def test_bare_ids_pass_but_paraphrased_statements_still_fail(redacted_saved_response):
    payload, rejected = redacted_saved_response
    # Deliberately construct a separate valid synthetic control; never repair provider output.
    control = deepcopy(rejected)
    allowed = evidence_context(payload)["observation_ids"]
    for barrier in control["stated_barriers"]:
        barrier["evidence"], = [v for v in allowed if barrier["evidence"].startswith(v + " ")]
    for strategy in control["activation_strategies"]:
        strategy["evidence_basis"] = [
            next(v for v in allowed if ref.startswith(v + " ")) for ref in strategy["evidence_basis"]
        ]
    AnalysisResponse.model_validate(control, context=evidence_context(payload))
    control["stated_barriers"][0]["barrier"] = "A paraphrased synthetic statement"
    with pytest.raises(ValidationError, match="Stated barriers must copy"):
        AnalysisResponse.model_validate(control, context=evidence_context(payload))
    assert rejected["stated_barriers"][0]["evidence"].endswith(" [EXPLANATION REDACTED]")

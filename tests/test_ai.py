import json
from typing import Annotated

from fastapi import Depends
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.ai.client import AIUnavailableError, MockAIClient
from app.dependencies import get_ai_client, get_ai_service
from app.main import create_app
from app.models.db import AIExtraction
from app.models.schemas.ai import AnalysisResponse
from app.services.ai_analysis import AIAnalysisService


class StubClient:
    model_name = "mock-invalid-response-fixture"
    data_source = "mock"

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def extract_and_strategise(self, payload):
        if self.error:
            raise self.error
        return self.response


async def test_mock_response_deterministic_valid_and_explicit(analysis_request):
    client = MockAIClient()
    first = await client.extract_and_strategise(analysis_request)
    assert first == await client.extract_and_strategise(analysis_request)
    output = AnalysisResponse.model_validate_json(first, context={"observation_ids": [item.id for item in analysis_request.observations]})
    assert output.activation_strategies[0].evidence_basis == ["synthetic-observation-001"]
    assert "MOCK" in output.uncertainty_statement
    assert "MOCK_OUTPUT" in output.uncertainty_flags


async def test_mock_order_does_not_change_response(analysis_request):
    second = analysis_request.observations[0].model_copy(update={"id": "synthetic-observation-002"})
    request = analysis_request.model_copy(update={"observations": [second, analysis_request.observations[0]]})
    reordered = request.model_copy(update={"observations": list(reversed(request.observations))})
    client = MockAIClient()
    assert await client.extract_and_strategise(request) == await client.extract_and_strategise(reordered)


def install_test_route(app, payload):
    @app.get("/_test/ai")
    async def probe(service: Annotated[AIAnalysisService, Depends(get_ai_service)]):
        result = await service.analyze(payload)
        return {"model": result.provenance.model_name, "status": result.provenance.validation_status}


def test_mock_resolved_through_fastapi_dependency(settings, analysis_request, monkeypatch):
    monkeypatch.setenv("USE_MOCK_AI", "true")
    app = create_app(settings)
    install_test_route(app, analysis_request)
    with TestClient(app) as client:
        assert isinstance(app.state.ai_client, MockAIClient)
        assert client.get("/_test/ai").json() == {"model": "mock-deterministic-v1", "status": "EXTRACTION_VALID"}


def test_client_can_be_overridden_using_fastapi_dependency(settings, analysis_request):
    app = create_app(settings)
    install_test_route(app, analysis_request)
    app.dependency_overrides[get_ai_client] = lambda: StubClient(response="not JSON")
    with TestClient(app) as client:
        assert client.get("/_test/ai").json() == {"model": "mock-invalid-response-fixture", "status": "EXTRACTION_FAILED"}


def test_factory_injection(settings):
    stub = StubClient(response="{}")
    assert create_app(settings, ai_client=stub).state.ai_client is stub


async def test_successful_provenance_round_trip(participant, analysis_request):
    result = await AIAnalysisService(MockAIClient()).analyze(analysis_request)
    assert result.output is not None
    async with participant.transaction() as session:
        session.add(result.provenance)
    async with participant.transaction() as session:
        record = (await session.scalars(select(AIExtraction))).one()
        assert record.raw_request == analysis_request.model_dump_json()
        assert record.raw_response == result.output.model_dump_json()
        assert record.parsed_output == result.output.model_dump(mode="json")
        assert record.validation_status == "EXTRACTION_VALID"
        assert record.parse_status == "PARSED"
        assert record.model_name == "mock-deterministic-v1"
        assert record.prompt_version == "milestone-1-contract-v1"
        assert record.observation_ids == ["synthetic-observation-001"]
        assert record.data_source == "mock"
        assert record.created_at.endswith("+00:00")


@pytest.mark.parametrize("mutation", [
    "empty_evidence", "unknown_evidence", "unknown_barrier_evidence", "empty_uncertainty",
    "missing_uncertainty_flags", "missing_pattern", "confidence_high", "confidence_negative",
    "confidence_nan", "string_confidence", "extra_field", "no_strategies",
])
async def test_semantically_invalid_json_rejected_with_provenance(analysis_request, mutation):
    raw = json.loads(await MockAIClient().extract_and_strategise(analysis_request))
    strategy = raw["activation_strategies"][0]
    if mutation == "empty_evidence": strategy["evidence_basis"] = []
    elif mutation == "unknown_evidence": strategy["evidence_basis"] = ["invented-observation"]
    elif mutation == "unknown_barrier_evidence": raw["stated_barriers"][0]["evidence"] = "invented"
    elif mutation == "empty_uncertainty": raw["uncertainty_statement"] = "   "
    elif mutation == "missing_uncertainty_flags": del raw["uncertainty_flags"]
    elif mutation == "missing_pattern": del raw["pattern_summary"]
    elif mutation == "confidence_high": strategy["confidence"] = 1.1
    elif mutation == "confidence_negative": strategy["confidence"] = -0.1
    elif mutation == "confidence_nan": strategy["confidence"] = float("nan")
    elif mutation == "string_confidence": strategy["confidence"] = "0.5"
    elif mutation == "extra_field": raw["purchase_verified"] = True
    elif mutation == "no_strategies": raw["activation_strategies"] = []
    response = json.dumps(raw)
    result = await AIAnalysisService(StubClient(response=response)).analyze(analysis_request)
    assert result.output is None
    assert result.provenance.validation_status == "EXTRACTION_INVALID"
    assert result.provenance.parse_status == "PARSED"
    assert result.provenance.raw_response == response
    assert result.provenance.parsed_output is None
    assert result.provenance.error


@pytest.mark.parametrize("response", ["{broken", "", "[]", "null", '{"unrelated": true}'])
async def test_malformed_or_wrong_shape_ai_response_is_explicit(analysis_request, response):
    result = await AIAnalysisService(StubClient(response=response)).analyze(analysis_request)
    assert result.output is None
    assert result.provenance.validation_status in {"EXTRACTION_FAILED", "EXTRACTION_INVALID"}
    assert result.provenance.raw_response == response
    assert result.provenance.error


async def test_provider_unavailable_never_fabricates_mock_output(analysis_request):
    result = await AIAnalysisService(StubClient(error=AIUnavailableError("fixture provider unavailable"))).analyze(analysis_request)
    assert result.output is None
    assert result.provenance.validation_status == "UNEXTRACTED"
    assert result.provenance.parse_status == "NOT_ATTEMPTED"
    assert result.provenance.raw_response is None
    assert result.provenance.parsed_output is None
    assert result.provenance.error == "fixture provider unavailable"


async def test_failure_provenance_can_be_saved(participant, analysis_request):
    result = await AIAnalysisService(StubClient(response="{broken")).analyze(analysis_request)
    async with participant.transaction() as session:
        session.add(result.provenance)
    async with participant.transaction() as session:
        record = (await session.scalars(select(AIExtraction))).one()
        assert record.validation_status == "EXTRACTION_FAILED"
        assert record.raw_response == "{broken"
        assert record.parsed_output is None


def test_duplicate_observation_ids_rejected(analysis_request):
    from app.models.schemas.ai import AnalysisRequest

    payload = analysis_request.model_dump()
    payload["observations"] *= 2
    with pytest.raises(ValidationError, match="unique"):
        AnalysisRequest.model_validate(payload)

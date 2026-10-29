import asyncio
import json

import httpx
from pydantic import SecretStr, ValidationError
import pytest

from app.ai.client import MockAIClient
from app.ai.meta import MetaAIClient, MetaProviderError
from app.ai.prompt import INSTRUCTIONS
from app.config import Settings
from app.main import create_app
from app.services.ai_analysis import AIAnalysisService

FAKE_KEY = "offline-" + "fixture-only"


def meta_settings(settings, **changes):
    values = settings.model_dump()
    values.update(use_mock_ai=False, meta_live_enabled=False, model_api_key=SecretStr(FAKE_KEY))
    values.update(changes)
    return Settings(_env_file=None, **values)


async def envelope(payload, **changes):
    result = {
        "id": "synthetic-completion", "created": 1793152800, "object": "chat.completion",
        "model": "muse-spark-1.3", "choices": [{
            "index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": await MockAIClient().extract_and_strategise(payload),
                "refusal": None,
            }, "logprobs": None,
        }],
        "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
    }
    result.update(changes)
    return result


async def test_meta_structured_request_and_complete_safe_trace(settings, analysis_request):
    calls = []
    async def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert str(request.url) == "https://api.meta.ai/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer " + FAKE_KEY
        assert body["max_completion_tokens"] == 2048
        assert body["messages"][0] == {"role": "developer", "content": INSTRUCTIONS}
        schema = body["response_format"]["json_schema"]
        assert schema["strict"] is True
        for value in [schema["schema"], *schema["schema"]["$defs"].values()]:
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
        return httpx.Response(200, json=await envelope(analysis_request), headers={"x-request-id": "synthetic-id"})
    client = MetaAIClient(meta_settings(settings), transport=httpx.MockTransport(handler))
    try:
        result = await AIAnalysisService(client).analyze(analysis_request)
        assert result.output is not None
        assert result.provenance.data_source == "meta_simulated"
        assert result.provenance.prompt_version == "meta-barriers-strategies-v1"
        details = result.provenance.call_details
        assert details["provider"] == "Meta Model API"
        assert details["inference_mode"] == "simulated"
        assert details["usage"]["total_tokens"] == 50
        assert details["provider_request_id"] == "synthetic-id"
        assert len(details["attempt_history"]) == len(calls) == 1
        assert FAKE_KEY not in json.dumps(details)
        assert "Authorization" not in json.dumps(details)
    finally:
        await client.aclose()
    assert client.is_closed


async def test_provider_client_closes_even_when_database_cleanup_fails(settings, monkeypatch):
    from app.database import Database
    original = Database.close
    async def fail_after_close(database):
        await original(database)
        raise RuntimeError("Synthetic disposal failure")
    app = create_app(meta_settings(settings, meta_live_enabled=True))
    monkeypatch.setattr(Database, "close", fail_after_close)
    with pytest.raises(RuntimeError, match="disposal failure"):
        async with app.router.lifespan_context(app):
            pass
    assert app.state.ai_client.is_closed
    assert app.state.ai_client.calls_reserved == 0


def test_model_copy_cannot_bypass_the_trusted_endpoint_check(settings):
    altered = meta_settings(settings).model_copy(update={"meta_base_url": "https://example.invalid"})
    with pytest.raises(ValidationError):
        MetaAIClient(altered, transport=httpx.MockTransport(lambda request: httpx.Response(500)))

@pytest.mark.parametrize("status,category", [
    (400, "bad_request"), (401, "authentication"), (403, "forbidden"),
    (429, "rate_limited"), (500, "server_error"), (503, "server_error"), (302, "http_error"),
])
async def test_http_failures_never_fabricate_or_retry_ambiguous_calls(settings, analysis_request, status, category):
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "Bearer " + FAKE_KEY}},
                              headers={"location": "https://example.invalid"})
    client = MetaAIClient(meta_settings(settings), transport=httpx.MockTransport(handler))
    try:
        result = await AIAnalysisService(client).analyze(analysis_request)
        assert result.output is None
        assert result.provenance.validation_status == "UNEXTRACTED"
        assert result.provenance.call_details["error_category"] == category
        assert len(calls) == 1
        assert FAKE_KEY not in json.dumps(result.provenance.call_details)
        assert FAKE_KEY not in result.provenance.error
    finally:
        await client.aclose()


async def test_explicit_429_retry_is_bounded_and_preserves_attempts(settings, analysis_request):
    calls = []
    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "synthetic throttle"}, headers={"retry-after": "0"})
        return httpx.Response(200, json=await envelope(analysis_request))
    client = MetaAIClient(meta_settings(settings, meta_max_retries=1, meta_max_calls=2),
                          transport=httpx.MockTransport(handler))
    try:
        result = await AIAnalysisService(client).analyze(analysis_request)
        assert result.output is not None
        assert len(calls) == client.calls_reserved == 2
        assert [v["http_status"] for v in result.provenance.call_details["attempt_history"]] == [429, 200]
    finally:
        await client.aclose()


@pytest.mark.parametrize("delay", ["30", "-1", "nan", "inf", "invalid"])
async def test_retry_after_cannot_cause_early_or_unbounded_retry(settings, analysis_request, delay):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"error": "synthetic"}, headers={"retry-after": delay})
    client = MetaAIClient(meta_settings(settings, meta_max_retries=1), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(MetaProviderError, match="rate_limited"):
            await client.extract_and_strategise(analysis_request)
        assert len(calls) == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize("exception", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
                                      httpx.PoolTimeout, httpx.RemoteProtocolError])
async def test_transport_errors_are_safe_and_do_not_retry(settings, analysis_request, exception):
    calls = []
    def handler(request):
        calls.append(request)
        raise exception("Unsafe exception includes " + FAKE_KEY, request=request)
    client = MetaAIClient(meta_settings(settings, meta_max_retries=1), transport=httpx.MockTransport(handler))
    try:
        result = await AIAnalysisService(client).analyze(analysis_request)
        assert result.output is None
        assert len(calls) == 1
        assert FAKE_KEY not in result.provenance.error
    finally:
        await client.aclose()


async def test_overall_timeout_bounds_mock_transport_and_cleans_client(settings, analysis_request):
    calls = []
    async def handler(request):
        calls.append(request)
        await asyncio.Event().wait()
    client = MetaAIClient(meta_settings(settings, meta_total_timeout=0.02), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(MetaProviderError, match="timeout"):
            await client.extract_and_strategise(analysis_request)
        assert len(calls) == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize("fault,category", [
    ("refusal", "refusal"), ("length", "incomplete_generation"),
    ("tool_calls", "incomplete_generation"), ("empty", "empty_content"),
    ("model", "model_mismatch"), ("bad_envelope", "invalid_envelope"),
    ("bad_json", "invalid_envelope"), ("duplicate_json", "invalid_envelope"),
    ("invalid_usage", "invalid_usage"), ("usage_limit", "usage_limit_breach"),
    ("content_type", "invalid_content_type"), ("large", "response_too_large"),
])
async def test_meta_rejects_incompatible_refused_truncated_or_oversized_responses(settings, analysis_request, fault, category):
    async def handler(request):
        raw = await envelope(analysis_request)
        if fault == "refusal": raw["choices"][0]["message"]["refusal"] = "Synthetic refusal"
        if fault in {"length", "tool_calls"}: raw["choices"][0]["finish_reason"] = fault
        if fault == "empty": raw["choices"][0]["message"]["content"] = None
        if fault == "model": raw["model"] = "different-provider-model"
        if fault == "bad_envelope": raw["choices"] = []
        if fault == "invalid_usage": raw["usage"]["total_tokens"] = True
        if fault == "usage_limit": raw["usage"] = {"prompt_tokens": 1, "completion_tokens": 99999, "total_tokens": 100000}
        if fault == "bad_json": return httpx.Response(200, content=b"{", headers={"content-type": "application/json"})
        if fault == "duplicate_json": return httpx.Response(200, content=b'{"choices":[],"choices":[]}', headers={"content-type": "application/json"})
        if fault == "content_type": return httpx.Response(200, text="not json")
        if fault == "large": return httpx.Response(200, content=b"x" * 65537)
        return httpx.Response(200, json=raw)
    client = MetaAIClient(meta_settings(settings), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(MetaProviderError, match=category):
            await client.extract_and_strategise(analysis_request)
        assert client.call_trace()["error_category"] == category
    finally:
        await client.aclose()


@pytest.mark.parametrize("mutation", ["json", "schema", "evidence", "product", "stated_fact"])
async def test_structured_output_is_independently_validated(settings, analysis_request, mutation):
    async def handler(request):
        outer = await envelope(analysis_request)
        content = json.loads(outer["choices"][0]["message"]["content"])
        if mutation == "schema": content["purchase_verified"] = True
        if mutation == "evidence": content["activation_strategies"][0]["evidence_basis"] = ["cross-route-id"]
        if mutation == "product": content["extracted_products"] = ["Invented unsupported product"]
        if mutation == "stated_fact": content["stated_barriers"][0]["barrier"] = "Invented retailer statement"
        outer["choices"][0]["message"]["content"] = "{" if mutation == "json" else json.dumps(content)
        return httpx.Response(200, json=outer)
    client = MetaAIClient(meta_settings(settings), transport=httpx.MockTransport(handler))
    try:
        result = await AIAnalysisService(client).analyze(analysis_request)
        assert result.output is None
        assert result.provenance.validation_status in {"EXTRACTION_FAILED", "EXTRACTION_INVALID"}
        assert result.provenance.raw_response is not None
    finally:
        await client.aclose()


async def test_call_token_and_input_budgets_prevent_network_calls(settings, analysis_request):
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json=await envelope(analysis_request))
    for changes in [{"meta_max_calls": 1}, {"meta_max_total_tokens": 64},
                    {"meta_max_request_bytes": 1024}]:
        client = MetaAIClient(meta_settings(settings, **changes), transport=httpx.MockTransport(handler))
        try:
            before = len(calls)
            if changes.get("meta_max_calls"):
                await client.extract_and_strategise(analysis_request)
                before += 1
            with pytest.raises(MetaProviderError, match="budget_exhausted|input_too_large"):
                await client.extract_and_strategise(analysis_request)
            assert len(calls) == before
        finally:
            await client.aclose()


async def test_concurrent_calls_are_bounded_and_traces_do_not_mix(settings, analysis_request):
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum = 0
    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        entered.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, json=await envelope(analysis_request),
                              headers={"x-request-id": str(len(request.content))})
    client = MetaAIClient(meta_settings(settings, meta_max_calls=2), transport=httpx.MockTransport(handler))
    async def call(payload):
        await client.extract_and_strategise(payload)
        return client.call_trace()["provider_request"]["messages"][1]["content"]
    second = analysis_request.model_copy(update={"route_baseline_id": "synthetic-other-route"})
    first_task = asyncio.create_task(call(analysis_request))
    await asyncio.wait_for(entered.wait(), 3)
    second_task = asyncio.create_task(call(second))
    release.set()
    try:
        values = await asyncio.gather(first_task, second_task)
        assert maximum == 1
        assert "synthetic-other-route" not in values[0]
        assert "synthetic-other-route" in values[1]
    finally:
        release.set()
        await client.aclose()


def test_secure_configuration_and_missing_or_disabled_live_credentials(settings):
    safe = meta_settings(settings)
    assert FAKE_KEY not in repr(safe)
    assert FAKE_KEY not in safe.model_dump_json()
    with pytest.raises(ValueError, match="mock-only"):
        MetaAIClient(safe)
    with pytest.raises(ValueError, match="required"):
        MetaAIClient(meta_settings(settings, meta_live_enabled=True, model_api_key=None))
    for endpoint in ["http://api.meta.ai/v1", "https://api.meta.ai.evil.test/v1", "https://example.invalid/v1"]:
        with pytest.raises(ValidationError) as captured:
            Settings(meta_base_url=endpoint, model_api_key=SecretStr(FAKE_KEY), _env_file=None)
        assert FAKE_KEY not in str(captured.value)


async def test_owned_live_adapter_lifecycle_without_making_a_call(settings):
    app = create_app(meta_settings(settings, meta_live_enabled=True))
    client = app.state.ai_client
    assert isinstance(client, MetaAIClient)
    assert client.inference_mode == "live"
    async with app.router.lifespan_context(app):
        assert client.calls_reserved == 0
    assert client.is_closed


async def test_injected_simulated_adapter_is_not_claimed_as_live_or_closed_by_owner(settings, analysis_request):
    async def handler(request):
        return httpx.Response(200, json=await envelope(analysis_request))
    client = MetaAIClient(meta_settings(settings), transport=httpx.MockTransport(handler))
    app = create_app(settings, ai_client=client)
    try:
        async with app.router.lifespan_context(app):
            assert app.state.ai_client.inference_mode == "simulated"
        assert not client.is_closed
    finally:
        await client.aclose()


async def test_escaped_secret_is_redacted_before_validation_and_provenance(settings, analysis_request):
    escaped = "".join("\\u%04x" % ord(character) for character in FAKE_KEY)
    async def handler(request):
        outer = await envelope(analysis_request)
        inner = json.loads(outer["choices"][0]["message"]["content"])
        inner["pattern_summary"] = FAKE_KEY
        outer["choices"][0]["message"]["content"] = json.dumps(inner).replace(FAKE_KEY, escaped)
        return httpx.Response(200, json=outer)
    client = MetaAIClient(meta_settings(settings), transport=httpx.MockTransport(handler))
    try:
        result = await AIAnalysisService(client).analyze(analysis_request)
        assert result.output is not None
        assert result.output.pattern_summary == "[REDACTED]"
        assert FAKE_KEY not in json.dumps(result.provenance.parsed_output)
        saved_outer = json.loads(result.provenance.call_details["raw_provider_response"])
        saved_inner = json.loads(saved_outer["choices"][0]["message"]["content"])
        assert saved_inner["pattern_summary"] == "[REDACTED]"
    finally:
        await client.aclose()


async def test_retry_failure_does_not_misattribute_previous_response(settings, analysis_request):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "synthetic throttle"}, headers={"retry-after": "0"})
        raise httpx.ReadTimeout("Synthetic retry timeout", request=request)
    client = MetaAIClient(meta_settings(settings, meta_max_retries=1), transport=httpx.MockTransport(handler))
    try:
        result = await AIAnalysisService(client).analyze(analysis_request)
        assert result.output is None and len(calls) == 2
        details = result.provenance.call_details
        assert details["attempt_history"][0]["http_status"] == 429
        assert details["error_category"] == "timeout"
        assert details["raw_provider_response"] is None
        assert details["http_status"] is None
    finally:
        await client.aclose()



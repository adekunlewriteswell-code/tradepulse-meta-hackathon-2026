import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
import subprocess
import sys
from types import SimpleNamespace

import httpx

from app.config import WORKSPACE_ROOT
from app.models.schemas.ai import AnalysisRequest, AnalysisResponse
from app.services.seeding import SEED_ROUTE_ID
from data import analyze_observations as workflow


def invoke(path, *arguments):
    environment = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{path}",
                       USE_MOCK_AI="true", META_LIVE_ENABLED="false",
                       PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("MODEL_API_KEY", None)
    return subprocess.run([sys.executable, "-B", "-m", "data.analyze_observations", *arguments],
                          cwd=WORKSPACE_ROOT, env=environment, capture_output=True, timeout=30)


def test_offline_cli_persists_observations_and_replays_mock_and_simulated_modes(tmp_path):
    path = tmp_path / "workflow.db"
    first = invoke(path, "--mode", "mock", "--seed-observations", "--analysis-key", "cli-mock")
    assert first.returncode == 0, first.stderr.decode("utf-8")
    assert "Inference mode: mock" in first.stdout.decode("utf-8")
    assert "\u20a62,000,000.00" in first.stdout.decode("utf-8")
    replay = invoke(path, "--mode", "mock", "--analysis-key", "cli-mock")
    assert replay.returncode == 0
    simulated = invoke(path, "--mode", "simulated", "--analysis-key", "cli-simulated")
    assert simulated.returncode == 0, simulated.stderr.decode("utf-8")
    assert "Inference mode: simulated" in simulated.stdout.decode("utf-8")
    assert "SIMULATED META HTTP RESPONSE" in simulated.stdout.decode("utf-8")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 8
        assert connection.execute("SELECT count(*) FROM ai_extractions").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM purchase_events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM reward_releases").fetchone()[0] == 0
        assert connection.execute("SELECT currency,monthly_revenue_minor FROM route_baselines").fetchone() == ("NGN", 200000000)


def test_live_cli_without_explicit_authorization_creates_no_database(tmp_path):
    path = tmp_path / "must-not-exist.db"
    result = invoke(path, "--mode", "live", "--analysis-key", "not-authorized")
    assert result.returncode == 2
    assert b"separate explicit authorization" in result.stdout
    assert not path.exists()


def test_live_cli_without_enabled_gate_fails_without_a_call_or_database(tmp_path):
    path = tmp_path / "still-disabled.db"
    result = invoke(path, "--mode", "live", "--live-authorized", "--analysis-key", "gate-disabled")
    assert result.returncode == 2
    assert not path.exists()


def test_simulated_cli_4096_budget_persists_valid_evidence_and_replays(tmp_path):
    path = tmp_path / "bounded-simulation.db"
    arguments = (
        "--mode", "simulated", "--analysis-key", "cli-simulated-4096",
        "--call-limit", "1", "--token-budget", "12000", "--output-token-limit", "4096",
    )
    first = invoke(path, *arguments, "--seed-observations")
    assert first.returncode == 0, first.stderr.decode("utf-8")
    replay = invoke(path, *arguments)
    assert replay.returncode == 0, replay.stderr.decode("utf-8")
    assert first.stdout == replay.stdout
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        record, = connection.execute("SELECT * FROM ai_extractions").fetchall()
        assert record["data_source"] == "meta_simulated"
        assert record["run_state"] == "DONE"
        assert record["validation_status"] == "EXTRACTION_VALID"
        details = json.loads(record["call_details"])
        assert details["provider_request"]["reasoning_effort"] == "low"
        assert details["provider_request"]["max_completion_tokens"] == 4096
        assert details["provider_request"]["response_format"]["json_schema"]["strict"] is True
        assert details["attempts"] == len(details["attempt_history"]) == 1
        payload = AnalysisRequest.model_validate_json(record["raw_request"])
        output = AnalysisResponse.model_validate_json(record["parsed_output"], context={
            "observation_ids": [v.id for v in payload.observations],
            "products": {p for v in payload.observations for p in v.products},
            "barriers": {v.id: v.stated_barrier for v in payload.observations},
        })
        assert "SIMULATED META HTTP RESPONSE" in output.uncertainty_statement


def test_cli_rejects_completion_budget_above_existing_4096_limit(tmp_path):
    path = tmp_path / "over-limit.db"
    result = invoke(path, "--mode", "simulated", "--analysis-key", "over-limit",
                    "--output-token-limit", "4097")
    assert result.returncode == 2
    assert b"ValidationError" in result.stdout
    assert not path.exists()


async def test_live_cli_4096_budget_forces_zero_retries_with_mocked_transport(tmp_path, monkeypatch):
    path = tmp_path / "mocked-live-cli.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setenv("MODEL_API_KEY", "offline-cli-fixture-only")
    monkeypatch.setenv("META_LIVE_ENABLED", "true")
    monkeypatch.setenv("META_MAX_RETRIES", "1")
    calls, clients = [], []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["reasoning_effort"] == "low"
        assert body["max_completion_tokens"] == 4096
        return httpx.Response(429, json={"error": "synthetic throttle"}, headers={"retry-after": "0"})

    class OfflineMetaAIClient(workflow.MetaAIClient):
        def __init__(self, settings, *, transport=None):
            assert transport is None
            assert settings.meta_max_retries == 0
            assert settings.meta_max_calls == 1
            assert settings.meta_max_total_tokens == 12000
            assert settings.meta_max_output_tokens == 4096
            super().__init__(settings, transport=httpx.MockTransport(handler))
            clients.append(self)

    monkeypatch.setattr(workflow, "MetaAIClient", OfflineMetaAIClient)
    args = SimpleNamespace(
        mode="live", live_authorized=True, call_limit=1, token_budget=12000,
        output_token_limit=4096, seed_observations=True, observation_id=None,
        route_id=SEED_ROUTE_ID, analysis_key="mocked-live-4096",
    )
    assert await workflow.demonstrate(args) == 2
    assert len(calls) == len(clients) == clients[0].calls_reserved == 1
    assert clients[0].is_closed
    with closing(sqlite3.connect(path)) as connection:
        details, = connection.execute("SELECT call_details FROM ai_extractions").fetchone()
        details = json.loads(details)
        assert details["error_category"] == "rate_limited"
        assert details["attempts"] == len(details["attempt_history"]) == 1


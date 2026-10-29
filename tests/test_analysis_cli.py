import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
import subprocess
import sys

from app.config import WORKSPACE_ROOT


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


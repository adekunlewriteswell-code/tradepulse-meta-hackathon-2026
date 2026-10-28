from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, WORKSPACE_ROOT
from app.main import create_app


def test_environment_selects_mock_and_database(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AI", "true")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./data/environment-test.db")
    settings = Settings(_env_file=None)
    assert settings.use_mock_ai is True
    assert "environment-test.db" in settings.database_url
    assert create_app(settings).state.ai_client.model_name == "mock-deterministic-v1"


def test_live_ai_mode_fails_closed(settings):
    with pytest.raises(ValueError, match="mock-only"):
        create_app(settings.model_copy(update={"use_mock_ai": False}))


@pytest.mark.parametrize("url", [
    "sqlite+aiosqlite:///:memory:", "postgresql://localhost/test",
    "sqlite+aiosqlite:///../outside-workspace.db",
    "sqlite+aiosqlite:///./data/test.db?mode=ro",
    "sqlite+aiosqlite:///./README.md",
])
def test_configuration_rejects_unsupported_or_external_database(url):
    with pytest.raises((ValidationError, ValueError)):
        Settings(database_url=url, _env_file=None)


def test_env_example_contains_no_required_secret():
    example = (WORKSPACE_ROOT / ".env.example").read_text(encoding="utf-8")
    settings = Settings(_env_file=WORKSPACE_ROOT / ".env.example")
    assert settings.use_mock_ai
    assert "API_KEY=" not in example


def test_ignored_runtime_and_secret_paths():
    ignore = (WORKSPACE_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", "!.env.example", ".venv/", "*.db", "*.db-*", "*.pem", "*.key"}.issubset(ignore)
    assert not (WORKSPACE_ROOT / ".env").exists()

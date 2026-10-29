"""Environment settings; database access is restricted to this workspace."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=WORKSPACE_ROOT / ".env", env_file_encoding="utf-8", extra="forbid",
        hide_input_in_errors=True,
    )

    database_url: str = "sqlite+aiosqlite:///./data/tradepulse.db"
    use_mock_ai: bool = True
    model_api_key: SecretStr | None = Field(default=None, repr=False)
    meta_live_enabled: bool = False
    meta_base_url: Literal["https://api.meta.ai/v1"] = "https://api.meta.ai/v1"
    meta_model: str = Field(default="muse-spark-1.3", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,99}$")
    meta_connect_timeout: float = Field(default=5.0, gt=0, le=30)
    meta_read_timeout: float = Field(default=20.0, gt=0, le=60)
    meta_total_timeout: float = Field(default=30.0, gt=0, le=120)
    meta_max_response_bytes: int = Field(default=65536, ge=256, le=262144)
    meta_max_request_bytes: int = Field(default=16384, ge=1024, le=65536)
    meta_max_output_tokens: int = Field(default=2048, ge=64, le=4096)
    meta_max_concurrency: int = Field(default=1, ge=1, le=4)
    meta_max_calls: int = Field(default=3, ge=1, le=20)
    meta_max_total_tokens: int = Field(default=60000, ge=64, le=1000000)
    meta_max_retries: int = Field(default=0, ge=0, le=1)

    @field_validator("database_url")
    @classmethod
    def local_sqlite_only(cls, value: str) -> str:
        url = make_url(value)
        if (url.drivername != "sqlite+aiosqlite" or url.host or url.username
                or url.password or url.query or not url.database
                or url.database == ":memory:"):
            raise ValueError("Use a file-backed sqlite+aiosqlite database in this workspace")
        path = Path(url.database)
        resolved = (WORKSPACE_ROOT / path).resolve()
        if not resolved.is_relative_to(WORKSPACE_ROOT):
            raise ValueError("Database must remain inside the hackathon workspace")
        if resolved.suffix not in {".db", ".sqlite", ".sqlite3"}:
            raise ValueError("Database filename must end in .db, .sqlite or .sqlite3")
        return url.set(database=str(resolved)).render_as_string(hide_password=False)

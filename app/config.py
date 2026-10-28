"""Environment settings; database access is restricted to this workspace."""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=WORKSPACE_ROOT / ".env", env_file_encoding="utf-8", extra="forbid"
    )

    database_url: str = "sqlite+aiosqlite:///./data/tradepulse.db"
    use_mock_ai: bool = True

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

"""Explicit, backed-up correction of the original synthetic currency fixture."""
from contextlib import closing
from pathlib import Path
import sqlite3

from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url

from app.config import WORKSPACE_ROOT
from app.database import Database
from app.models.db import Retailer, RouteBaseline
from app.services.seeding import SEED_ROUTE_ID, synthetic_rows


class CurrencyCorrectionError(ValueError):
    pass


def legacy_baseline() -> dict:
    row = synthetic_rows()[0].copy()
    row["currency"] = "GBP"
    for name in row:
        if name.endswith("_minor"):
            row[name] //= 1000
    return row


def backup_database(database: Database, destination: Path) -> None:
    destination = destination.resolve()
    if not destination.is_relative_to(WORKSPACE_ROOT) or destination.exists():
        raise CurrencyCorrectionError("Backup must be a new file inside this workspace")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = Path(make_url(database.engine.url).database)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as backup:
            src.backup(backup)


async def correct_synthetic_currency(database: Database, backup: Path) -> bool:
    """Refuse anything except the intact original fixture; repeat is a no-op."""
    expected, retailers = synthetic_rows()
    backup = backup.resolve()
    if not backup.is_relative_to(WORKSPACE_ROOT) or not backup.is_file():
        raise CurrencyCorrectionError("A local SQLite backup is required")
    async with database.transaction() as session:
        routes = [dict(v) for v in (await session.execute(select(RouteBaseline.__table__))).mappings()]
        if routes == [expected]:
            return False
        actual_retailers = [dict(v) for v in (await session.execute(
            select(Retailer.__table__).order_by(Retailer.id))).mappings()]
        if routes != [legacy_baseline()] or actual_retailers != retailers:
            raise CurrencyCorrectionError("Only the intact original synthetic fixture can be corrected")
        for table in ("observations", "commitments", "missions", "purchase_events",
                      "reward_releases", "audit_logs", "ai_extractions"):
            if (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one():
                raise CurrencyCorrectionError("Existing activity requires explicit review; no correction made")
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as saved:
            saved.row_factory = sqlite3.Row
            if ([dict(v) for v in saved.execute("SELECT * FROM route_baselines")] != routes
                    or [dict(v) for v in saved.execute("SELECT * FROM retailers ORDER BY id")] != actual_retailers):
                raise CurrencyCorrectionError("Backup does not match the original fixture")
            if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CurrencyCorrectionError("Backup integrity check failed")
            if saved.execute("PRAGMA foreign_key_check").fetchall():
                raise CurrencyCorrectionError("Backup foreign-key check failed")
        changes = {k: v for k, v in expected.items() if k == "currency" or k.endswith("_minor")}
        await session.execute(update(RouteBaseline).where(RouteBaseline.id == SEED_ROUTE_ID).values(**changes))
    return True


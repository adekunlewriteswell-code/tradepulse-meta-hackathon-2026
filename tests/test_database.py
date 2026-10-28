import sqlite3

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url

from app.models.db import AuditLog, Base, Retailer


async def test_schema_complete_and_creation_repeatable(database):
    await database.create_schema()
    async with database.engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        assert set(tables) == set(Base.metadata.tables) == {
            "retailers", "route_baselines", "observations", "commitments", "missions",
            "purchase_events", "reward_releases", "audit_logs", "ai_extractions",
        }
        assert (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all() == []


async def test_pragmas_on_independent_connections(database):
    async with database.engine.connect() as first, database.engine.connect() as second:
        for conn in (first, second):
            assert (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar_one() == "wal"
            assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
            assert (await conn.exec_driver_sql("PRAGMA recursive_triggers")).scalar_one() == 1
            assert (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar_one() == 5000


async def test_foreign_key_failure_rolls_back_all_writes(database):
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        async with database.transaction() as session:
            session.add(AuditLog(id="rollback-audit", event="TEST", actor="synthetic", details={}, data_source="synthetic"))
            await session.flush()
            session.add(Retailer(id="orphan", route_baseline_id="missing", code="SYNTHETIC-ORPHAN", name="Synthetic orphan", status="INACTIVE", data_source="synthetic"))
    async with database.transaction() as session:
        assert await session.get(AuditLog, "rollback-audit") is None
        assert await session.get(Retailer, "orphan") is None
        session.add(AuditLog(id="after-rollback", event="TEST", actor="synthetic", details={}, data_source="synthetic"))
    async with database.transaction() as session:
        assert await session.get(AuditLog, "after-rollback") is not None


async def test_explicit_transaction_rolls_back_ddl(database):
    with pytest.raises(RuntimeError, match="abort"):
        async with database.engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE rollback_probe (id INTEGER)")
            raise RuntimeError("abort")
    async with database.engine.connect() as conn:
        assert (await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE name='rollback_probe'")).all() == []


async def test_audit_protections_apply_to_direct_sqlite_connection(database, settings):
    async with database.transaction() as session:
        session.add(AuditLog(id="immutable", event="TEST", actor="synthetic", details={"total_minor": 100}, data_source="synthetic"))
    # Independent sqlite3 connections have recursive_triggers OFF by default.
    connection = sqlite3.connect(make_url(settings.database_url).database)
    try:
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        for sql in (
            "UPDATE audit_logs SET actor='changed' WHERE id='immutable'",
            "DELETE FROM audit_logs WHERE id='immutable'",
            "INSERT OR REPLACE INTO audit_logs SELECT * FROM audit_logs WHERE id='immutable'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(sql)
            connection.rollback()
        assert connection.execute("SELECT actor FROM audit_logs WHERE id='immutable'").fetchone() == ("synthetic",)
    finally:
        connection.close()


@pytest.mark.parametrize("operation", ["UPDATE audit_logs SET actor='changed'", "DELETE FROM audit_logs"])
async def test_audit_update_delete_rejected_by_database(database, operation):
    async with database.transaction() as session:
        session.add(AuditLog(id="audit-1", event="TEST", actor="synthetic", details={}, data_source="synthetic"))
    with pytest.raises(IntegrityError, match="append-only"):
        async with database.transaction() as session:
            await session.execute(text(operation))
    async with database.transaction() as session:
        assert (await session.scalars(select(AuditLog))).one().actor == "synthetic"


async def test_audit_json_rejects_floating_point_money(database):
    with pytest.raises(IntegrityError, match="exact integers"):
        async with database.transaction() as session:
            session.add(AuditLog(event="TEST", actor="synthetic", details={"nested": {"money": 1.25}}, data_source="synthetic"))


async def test_audit_orm_update_and_delete_rejected(database):
    async with database.transaction() as session:
        session.add(AuditLog(id="orm-immutable", event="TEST", actor="synthetic", details={}, data_source="synthetic"))
    with pytest.raises(IntegrityError, match="append-only"):
        async with database.transaction() as session:
            record = await session.get(AuditLog, "orm-immutable")
            record.actor = "changed"
    with pytest.raises(IntegrityError, match="append-only"):
        async with database.transaction() as session:
            await session.delete(await session.get(AuditLog, "orm-immutable"))

"""Data preservation and validation at ORM, direct-SQL and legacy boundaries."""

from datetime import UTC, datetime
from contextlib import closing
from decimal import Decimal, localcontext
import random
import sqlite3

import httpx
import httpx2
import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, MetaData, text
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateTable
from sqlalchemy.exc import IntegrityError

from app.database import Database
from app.domain.money import from_minor_units, to_minor_units
from app.models.db import Base, Mission
from app.models.schemas.mission import MissionMetadata
from app.services.seeding import SEED_ROUTE_ID, SEED_TIMESTAMP, synthetic_rows


def make_legacy_database(settings, *, custom_index=False, invalid_mission=False, invalid_balance=False):
    """Reconstruct the original audited schema; no backup artifact dependency."""
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    metadata.tables['audit_logs'].dialect_options['sqlite']['with_rowid'] = True
    mission = metadata.tables['missions']
    mission.constraints.remove(next(c for c in mission.constraints if c.name == 'ck_missions_metadata_valid'))
    route = metadata.tables['route_baselines']
    route.constraints.remove(next(c for c in route.constraints if c.name == 'ck_route_baselines_contribution_consistent'))
    route.append_constraint(CheckConstraint(
        'monthly_contribution_minor = monthly_revenue_minor - monthly_cogs_minor - monthly_fulfillment_minor - monthly_incentive_minor - monthly_other_costs_minor',
        name='legacy_balance',
    ))
    connection = sqlite3.connect(make_url(settings.database_url).database)
    for table in metadata.sorted_tables:
        connection.execute(str(CreateTable(table).compile(dialect=dialect())))
    baseline = synthetic_rows()[0]
    connection.execute('INSERT INTO route_baselines ('+','.join(baseline)+') VALUES ('+','.join('?' for _ in baseline)+')', tuple(baseline.values()))
    for rowid, identifier in [(-7, 'legacy-negative-rowid'), (42, 'legacy-positive-rowid')]:
        connection.execute("""INSERT INTO audit_logs
            (rowid,id,created_at,data_source,event,actor,details,source_event_id,idempotency_key)
            VALUES (?, ?, ?, 'synthetic', 'TEST', 'synthetic-manager', '{"total_minor":100}', 'synthetic-source', 'synthetic-key')
        """, (rowid,identifier,SEED_TIMESTAMP))
    connection.execute("CREATE TRIGGER audit_logs_no_delete BEFORE DELETE ON audit_logs BEGIN SELECT RAISE(ABORT,'audit_logs are append-only'); END")
    if custom_index:
        connection.execute('CREATE INDEX custom_audit_index ON audit_logs(actor)')
    if invalid_mission:
        connection.execute("""INSERT INTO missions
            (id,created_at,data_source,route_baseline_id,name,qualifying_products,target,minimum_quantity,
             starts_at,ends_at,incentive_bps,margin_guardrail_bps,status,created_by)
            VALUES ('legacy-mission',?,'synthetic',?,'Synthetic legacy mission','[]',6,1,
                    '2026-10-27T09:30:00+00:00','2026-10-27T10:00:00+01:00',500,1000,'DRAFT','synthetic-manager')
        """, (SEED_TIMESTAMP,SEED_ROUTE_ID))
    if invalid_balance:
        connection.execute('UPDATE route_baselines SET monthly_revenue_minor=0,monthly_cogs_minor=9223372036854775807,monthly_fulfillment_minor=2,monthly_incentive_minor=0,monthly_other_costs_minor=0,monthly_contribution_minor=-9223372036854775808')
    connection.commit()
    rows = connection.execute('SELECT * FROM audit_logs ORDER BY id').fetchall()
    connection.close()
    return rows


async def test_legacy_audit_upgrade_preserves_all_values_and_is_repeatable(settings):
    before = make_legacy_database(settings)
    database = Database(settings)
    try:
        await database.create_schema()
        await database.create_schema()
    finally:
        await database.close()
    with closing(sqlite3.connect(make_url(settings.database_url).database)) as connection:
        assert connection.execute('SELECT * FROM audit_logs ORDER BY id').fetchall() == before
        assert 'WITHOUT ROWID' in connection.execute("SELECT sql FROM sqlite_master WHERE name='audit_logs'").fetchone()[0]
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        with pytest.raises(sqlite3.OperationalError, match='rowid'):
            connection.execute('SELECT rowid FROM audit_logs')
        for operation in ("UPDATE audit_logs SET actor='changed'", 'DELETE FROM audit_logs', 'INSERT OR REPLACE INTO audit_logs SELECT * FROM audit_logs'):
            with pytest.raises(sqlite3.IntegrityError, match='append-only'):
                connection.execute(operation)


@pytest.mark.parametrize('custom_index, invalid_mission, invalid_balance', [(True,False,False),(False,True,False),(False,False,True)])
async def test_unsafe_legacy_upgrade_rolls_back_schema_and_preserves_audits(settings, custom_index, invalid_mission, invalid_balance):
    before = make_legacy_database(settings, custom_index=custom_index, invalid_mission=invalid_mission, invalid_balance=invalid_balance)
    database = Database(settings)
    try:
        with pytest.raises(RuntimeError, match='review'):
            await database.create_schema()
    finally:
        await database.close()
    with closing(sqlite3.connect(make_url(settings.database_url).database)) as connection:
        assert connection.execute('SELECT * FROM audit_logs ORDER BY id').fetchall() == before
        assert 'WITHOUT ROWID' not in connection.execute("SELECT sql FROM sqlite_master WHERE name='audit_logs'").fetchone()[0]
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name='audit_logs_rebuild'").fetchone()[0] == 0


async def test_legacy_route_gets_exact_balance_guards_for_direct_sql(settings):
    make_legacy_database(settings)
    database = Database(settings)
    try:
        await database.create_schema()
    finally:
        await database.close()
    with closing(sqlite3.connect(make_url(settings.database_url).database)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match='remediation validation'):
            connection.execute('UPDATE route_baselines SET monthly_revenue_minor=0,monthly_cogs_minor=9223372036854775807,monthly_fulfillment_minor=2,monthly_incentive_minor=0,monthly_other_costs_minor=0,monthly_contribution_minor=-9223372036854775808')


@pytest.mark.parametrize('value, units', [
    ('-0.01',-1), ('-450.00',-45000), ('-92233720368547758.08',-(2**63)),
    ('92233720368547758.07',2**63-1), ('-0.00',0), ('1.2300',123),
])
def test_signed_money_round_trip_and_default_nonnegative_contract(value, units):
    assert to_minor_units(value, signed=True) == units
    assert from_minor_units(units, signed=True) == Decimal(value)
    if units < 0:
        with pytest.raises(ValueError): to_minor_units(value)
        with pytest.raises(ValueError): from_minor_units(units)


@pytest.mark.parametrize('value', ['-92233720368547758.09','92233720368547758.08','-0.001','NaN','sNaN','1e1000000','1e-1000000'])
def test_signed_money_rejects_overflow_nonfinite_and_fractional_units(value):
    with pytest.raises(ValueError): to_minor_units(value,signed=True)


def test_money_independent_of_all_ambient_decimal_context_limits():
    with localcontext() as context:
        context.prec=1
        context.Emax=1
        context.Emin=-1
        assert to_minor_units('-92233720368547758.08',signed=True) == -(2**63)
        assert from_minor_units(-(2**63),signed=True) == Decimal('-92233720368547758.08')


def test_mission_metadata_accepts_aware_datetime_and_normalizes_utc():
    metadata=MissionMetadata(starts_at='2026-10-27T10:00:00+01:00',ends_at=datetime(2026,10,27,10,tzinfo=UTC))
    assert metadata.starts_at == '2026-10-27T09:00:00.000000+00:00'
    assert metadata.ends_at == '2026-10-27T10:00:00.000000+00:00'


@pytest.mark.parametrize('changes', [
    {'starts_at':'2026-10-27T00:00:00'},
    {'ends_at':'2026-10-27T00:00:00+00:00'},
    {'starts_at':'2026-10-27T09:30:00+00:00','ends_at':'2026-10-27T10:00:00+01:00'},
    {'status':'ACTIVE'}, {'status':'ACTIVE','approved_by':' \t','approved_at':SEED_TIMESTAMP,'parameters_hash':'a'*64},
])
def test_application_metadata_validation_rejects_invalid_values(changes):
    values={'starts_at':SEED_TIMESTAMP,'ends_at':'2026-11-04T00:00:00.000000+00:00',**changes}
    with pytest.raises(ValidationError): MissionMetadata.model_validate(values)


async def test_orm_normalizes_mixed_offsets_before_storage(participant):
    async with participant.transaction() as session:
        mission=await session.get(Mission,'synthetic-mission-001')
        mission.starts_at='2026-10-27T10:00:00+01:00'
        mission.ends_at='2026-10-27T10:00:00+00:00'
        mission.approved_at='2026-10-27T09:00:00Z'
    async with participant.transaction() as session:
        row=(await session.execute(text('SELECT starts_at,ends_at,approved_at FROM missions'))).one()
        assert row == ('2026-10-27T09:00:00.000000+00:00','2026-10-27T10:00:00.000000+00:00','2026-10-27T09:00:00.000000+00:00')


async def test_orm_rejects_blank_approval_and_rolls_back(participant):
    with pytest.raises(ValidationError):
        async with participant.transaction() as session:
            mission=await session.get(Mission,'synthetic-mission-001')
            mission.approved_by=' \t\u2003'
    async with participant.transaction() as session:
        assert (await session.get(Mission,'synthetic-mission-001')).approved_by == 'synthetic-manager'


async def test_new_constraints_apply_without_application_connection_hooks(database, settings):
    with closing(sqlite3.connect(make_url(settings.database_url).database)) as connection:
        # Isolate the actual CREATE TABLE constraint from compatibility triggers.
        for name, in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='route_baselines'").fetchall():
            connection.execute(f'DROP TRIGGER {name}')
        row={**synthetic_rows()[0],'monthly_revenue_minor':0,'monthly_cogs_minor':2**63-1,
             'monthly_fulfillment_minor':2,'monthly_incentive_minor':0,'monthly_other_costs_minor':0,
             'monthly_contribution_minor':-(2**63)}
        with pytest.raises(sqlite3.IntegrityError,match='contribution_consistent'):
            connection.execute('INSERT INTO route_baselines ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))


async def test_baseline_guard_matches_python_exact_integer_arithmetic(seeded, settings):
    generator=random.Random(20261028)
    values=[0,1,2,100,2**63-2,2**63-1]
    with closing(sqlite3.connect(make_url(settings.database_url).database)) as connection:
        for _ in range(128):
            revenue=generator.choice(values)
            costs=[generator.choice(values) for _ in range(4)]
            exact=revenue-sum(costs)
            stored=max(-(2**63),min(2**63-1,exact))
            command='UPDATE route_baselines SET monthly_revenue_minor=?,monthly_cogs_minor=?,monthly_fulfillment_minor=?,monthly_incentive_minor=?,monthly_other_costs_minor=?,monthly_contribution_minor=?'
            parameters=(revenue,*costs,stored)
            if exact != stored:
                with pytest.raises(sqlite3.IntegrityError): connection.execute(command,parameters)
            else:
                connection.execute(command,parameters)
                assert connection.execute('SELECT monthly_contribution_minor,typeof(monthly_contribution_minor) FROM route_baselines').fetchone() == (exact,'integer')


@pytest.mark.parametrize('field,value', [('ends_at','2026-10-27T10:00:00+01:00'),('approved_by',' \t\u2003'),('parameters_hash',''),('starts_at','2026-02-30T00:00:00.000000+00:00')])
async def test_fresh_mission_check_works_without_compatibility_triggers(participant, settings, field, value):
    with closing(sqlite3.connect(make_url(settings.database_url).database)) as connection:
        for name, in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='missions'").fetchall():
            connection.execute(f'DROP TRIGGER {name}')
        with pytest.raises(sqlite3.IntegrityError,match='metadata_valid'):
            connection.execute(f'UPDATE missions SET {field}=?',(value,))


@pytest.mark.parametrize('library', [httpx,httpx2])
def test_network_guard_blocks_both_sync_client_backends(library):
    with library.Client() as client:
        with pytest.raises(AssertionError,match='Network HTTP transport forbidden'):
            client.get('https://example.invalid')


@pytest.mark.parametrize('library', [httpx,httpx2])
async def test_network_guard_blocks_both_async_client_backends(library):
    async with library.AsyncClient() as client:
        with pytest.raises(AssertionError,match='Network HTTP transport forbidden'):
            await client.get('https://example.invalid')

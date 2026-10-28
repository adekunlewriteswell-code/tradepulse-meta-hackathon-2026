"""Regression reproductions from the October 28 engineering audit."""

from decimal import Decimal
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.domain.money import to_minor_units
from app.models.db import AuditLog, RouteBaseline
from app.services.seeding import SEED_ROUTE_ID, SEED_TIMESTAMP, synthetic_rows


@pytest.mark.parametrize('recursive', [0, 1])
@pytest.mark.parametrize('identity', ['rowid', '_rowid_', 'oid'])
async def test_hidden_row_identity_cannot_replace_audit(database, settings, recursive, identity):
    async with database.transaction() as session:
        session.add(AuditLog(id='original', event='TEST', actor='synthetic',
                             details={'total_minor': 100}, data_source='synthetic'))
    connection = sqlite3.connect(make_url(settings.database_url).database)
    try:
        connection.execute(f'PRAGMA recursive_triggers={recursive}')
        original = connection.execute('SELECT * FROM audit_logs').fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(f"""
                INSERT OR REPLACE INTO audit_logs
                ({identity}, id, created_at, data_source, event, actor, details)
                SELECT {identity}, 'replacement', created_at, data_source, event, actor, details
                FROM audit_logs WHERE id='original'
            """)
        connection.rollback()
        assert connection.execute('SELECT * FROM audit_logs').fetchall() == original
    finally:
        connection.close()


@pytest.mark.parametrize('recursive', [0, 1])
@pytest.mark.parametrize('operation', [
    "UPDATE audit_logs SET actor='changed' WHERE id='original'",
    "DELETE FROM audit_logs WHERE id='original'",
    "INSERT OR REPLACE INTO audit_logs SELECT * FROM audit_logs WHERE id='original'",
    "INSERT INTO audit_logs SELECT * FROM audit_logs WHERE id='original' ON CONFLICT(id) DO UPDATE SET actor='changed'",
])
async def test_audit_dml_guards_with_independent_connections(database, settings, recursive, operation):
    async with database.transaction() as session:
        session.add(AuditLog(id='original', event='TEST', actor='synthetic', details={}, data_source='synthetic'))
    connection = sqlite3.connect(make_url(settings.database_url).database)
    try:
        connection.execute(f'PRAGMA recursive_triggers={recursive}')
        before = connection.execute('SELECT * FROM audit_logs').fetchall()
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            connection.execute(operation)
        connection.rollback()
        assert connection.execute('SELECT * FROM audit_logs').fetchall() == before
    finally:
        connection.close()


@pytest.mark.parametrize('costs', [
    (2**63 - 1, 2, 0, 0),
    (2**63 - 1, 1, 1, 0),
    (2**63 - 1, 1, 0, 1),
])
async def test_baseline_rejects_overflow_even_when_float_rounds_to_valid_integer(database, costs):
    row = {**synthetic_rows()[0], 'monthly_revenue_minor': 0,
           'monthly_cogs_minor': costs[0], 'monthly_fulfillment_minor': costs[1],
           'monthly_incentive_minor': costs[2], 'monthly_other_costs_minor': costs[3],
           'monthly_contribution_minor': -(2**63)}
    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(RouteBaseline(**row))


@pytest.mark.parametrize('contribution, revenue, cogs, fulfillment', [
    (-(2**63), 0, 2**63-1, 1),
    (-1, 0, 1, 0),
    (0, 100, 99, 1),
    (2**63-1, 2**63-1, 0, 0),
])
async def test_baseline_exact_signed_boundaries(database, contribution, revenue, cogs, fulfillment):
    row = {**synthetic_rows()[0], 'monthly_revenue_minor': revenue,
           'monthly_cogs_minor': cogs, 'monthly_fulfillment_minor': fulfillment,
           'monthly_incentive_minor': 0, 'monthly_other_costs_minor': 0,
           'monthly_contribution_minor': contribution}
    async with database.transaction() as session:
        session.add(RouteBaseline(**row))
    async with database.transaction() as session:
        assert (await session.get(RouteBaseline, SEED_ROUTE_ID)).monthly_contribution_minor == contribution


@pytest.mark.parametrize('value', ['1e1000000', '-1e1000000', '1e-1000000', 'sNaN', 'Infinity', '', 'malformed'])
def test_money_extreme_or_invalid_input_has_consistent_validation_error(value):
    with pytest.raises(ValueError):
        to_minor_units(value)


@pytest.mark.parametrize('field, value', [
    ('starts_at', '2026-10-27T09:00:00'),
    ('ends_at', '2026-10-27T10:00:00+01:00'),
    ('ends_at', '2026-10-27T00:00:00.000000+00:00'),
    ('ends_at', '2026-02-30T00:00:00.000000+00:00'),
    ('approved_by', ''), ('approved_by', ' \t\r\n'), ('approved_by', '\u2003\u00a0'),
    ('approved_at', ''), ('parameters_hash', ''), ('parameters_hash', '\t\u2003'),
])
async def test_direct_sql_mission_validation_rejects_invalid_metadata(participant, field, value):
    with pytest.raises(IntegrityError):
        async with participant.transaction() as session:
            await session.execute(text(f'UPDATE missions SET {field}=:value'), {'value': value})


async def test_original_mixed_offset_inverted_window_rejected(participant):
    with pytest.raises(IntegrityError):
        async with participant.transaction() as session:
            await session.execute(text("""UPDATE missions
                SET starts_at='2026-10-27T09:30:00+00:00',
                    ends_at='2026-10-27T10:00:00+01:00'
            """))

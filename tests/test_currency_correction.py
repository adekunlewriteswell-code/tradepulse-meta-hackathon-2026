from decimal import localcontext

import pytest
from sqlalchemy import select

from app.domain.money import format_ngn
from app.models.db import AuditLog, Retailer, RouteBaseline
from app.services.currency_correction import (
    CurrencyCorrectionError, backup_database, correct_synthetic_currency, legacy_baseline,
)
from app.services.seeding import SEED_ROUTE_ID, synthetic_rows


async def old_fixture(database):
    _, retailers = synthetic_rows()
    async with database.transaction() as session:
        session.add(RouteBaseline(**legacy_baseline()))
        await session.flush()
        session.add_all([Retailer(**row) for row in retailers])


async def test_ngn_amounts_and_kobo_are_exact(seeded):
    expected = {
        "monthly_revenue_minor": 200000000, "monthly_cogs_minor": 140000000,
        "monthly_fulfillment_minor": 10000000, "monthly_incentive_minor": 0,
        "monthly_other_costs_minor": 5000000, "monthly_contribution_minor": 45000000,
    }
    async with seeded.transaction() as session:
        route = await session.get(RouteBaseline, SEED_ROUTE_ID)
        assert route.currency == "NGN"
        assert all(getattr(route, key) == value and type(getattr(route, key)) is int
                   for key, value in expected.items())


def test_ngn_format_uses_exact_kobo_and_grouping():
    assert format_ngn(200000000) == "\u20a62,000,000.00"
    assert format_ngn(1999) == "\u20a619.99"
    with localcontext() as context:
        context.prec = 1
        assert format_ngn(-123456789) == "-\u20a61,234,567.89"
    with pytest.raises(TypeError):
        format_ngn(1.5)


async def test_currency_correction_is_backed_up_and_repeatable(database, tmp_path):
    await old_fixture(database)
    backup = tmp_path / "before.db"
    backup_database(database, backup)
    before = backup.read_bytes()
    assert await correct_synthetic_currency(database, backup) is True
    assert await correct_synthetic_currency(database, backup) is False
    assert backup.read_bytes() == before
    async with database.transaction() as session:
        route = (await session.execute(select(RouteBaseline.__table__))).mappings().one()
        assert dict(route) == synthetic_rows()[0]
        assert len((await session.scalars(select(Retailer))).all()) == 10


@pytest.mark.parametrize("change", ["status", "money", "activity", "provenance"])
async def test_currency_correction_refuses_changed_or_unrelated_data(database, tmp_path, change):
    await old_fixture(database)
    async with database.transaction() as session:
        if change == "status":
            (await session.get(Retailer, "synthetic-retailer-003")).status = "ACTIVE"
        elif change == "provenance":
            (await session.get(Retailer, "synthetic-retailer-003")).data_source = "other"
        elif change == "money":
            row = await session.get(RouteBaseline, SEED_ROUTE_ID)
            row.monthly_revenue_minor += 1
            row.monthly_contribution_minor += 1
        else:
            session.add(AuditLog(event="TEST", actor="synthetic", data_source="synthetic", details={}))
    backup = tmp_path / "before.db"
    backup_database(database, backup)
    with pytest.raises(CurrencyCorrectionError):
        await correct_synthetic_currency(database, backup)
    async with database.transaction() as session:
        assert (await session.get(RouteBaseline, SEED_ROUTE_ID)).currency == "GBP"


async def test_currency_correction_requires_matching_backup(database, tmp_path):
    await old_fixture(database)
    with pytest.raises(CurrencyCorrectionError):
        await correct_synthetic_currency(database, tmp_path / "missing.db")
    backup = tmp_path / "before.db"
    backup_database(database, backup)
    async with database.transaction() as session:
        route = await session.get(RouteBaseline, SEED_ROUTE_ID)
        route.name = "Changed synthetic route"
    with pytest.raises(CurrencyCorrectionError):
        await correct_synthetic_currency(database, backup)


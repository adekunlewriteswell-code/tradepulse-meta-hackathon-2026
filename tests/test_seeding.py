import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.db import Retailer, RouteBaseline
from app.services.seeding import SEED_ROUTE_ID, SeedConflictError, seed_synthetic, synthetic_rows


async def snapshot(database):
    async with database.transaction() as session:
        retailers = (await session.execute(select(Retailer.__table__).order_by(Retailer.id))).mappings().all()
        routes = (await session.execute(select(RouteBaseline.__table__).order_by(RouteBaseline.id))).mappings().all()
        return [dict(row) for row in routes], [dict(row) for row in retailers]


async def test_exact_seed_contents_and_all_records_synthetic(seeded):
    routes, retailers = await snapshot(seeded)
    assert len(routes) == 1
    assert len(retailers) == 10
    assert sum(row["status"] == "ACTIVE" for row in retailers) == 2
    assert sum(row["status"] == "INACTIVE" for row in retailers) == 8
    assert all(row["data_source"] == "synthetic" for row in routes + retailers)
    assert all("Synthetic" in row["name"] for row in routes + retailers)
    expected_route, expected_retailers = synthetic_rows()
    assert routes == [expected_route]
    assert retailers == expected_retailers


async def test_seed_is_byte_value_repeatable(seeded):
    before = await snapshot(seeded)
    await seed_synthetic(seeded)
    await seed_synthetic(seeded)
    assert await snapshot(seeded) == before


async def test_seed_is_deterministic_across_fresh_databases(seeded, settings):
    from app.config import Settings
    from app.database import Database
    second_settings = Settings(database_url=settings.database_url.replace("test.db", "second.db"), _env_file=None)
    second = Database(second_settings)
    try:
        await second.create_schema()
        await seed_synthetic(second)
        assert await snapshot(second) == await snapshot(seeded)
    finally:
        await second.close()


async def test_seed_fills_partial_dataset_without_overwriting(seeded):
    async with seeded.transaction() as session:
        await session.execute(text("DELETE FROM retailers WHERE id='synthetic-retailer-010'"))
        retailer = await session.get(Retailer, "synthetic-retailer-003")
        retailer.status = "ACTIVE"
    await seed_synthetic(seeded)
    _, retailers = await snapshot(seeded)
    assert len(retailers) == 10
    assert retailers[2]["status"] == "ACTIVE"


async def test_seed_failure_rolls_back_partial_inserts(database):
    route, rows = synthetic_rows()
    async with database.transaction() as session:
        session.add(RouteBaseline(**route))
        await session.flush()
        session.add(Retailer(**{**rows[0], "id": "synthetic-conflicting-row", "code": rows[5]["code"]}))
    with pytest.raises(IntegrityError):
        await seed_synthetic(database)
    routes, retailers = await snapshot(database)
    assert len(routes) == 1
    assert len(retailers) == 1
    assert retailers[0]["id"] == "synthetic-conflicting-row"


async def test_baseline_costs_contribution_and_integer_storage(seeded):
    async with seeded.transaction() as session:
        baseline = await session.get(RouteBaseline, SEED_ROUTE_ID)
        assert baseline.monthly_contribution_minor == 45000
        assert baseline.monthly_contribution_minor == baseline.monthly_revenue_minor - baseline.monthly_cogs_minor - baseline.monthly_fulfillment_minor - baseline.monthly_incentive_minor - baseline.monthly_other_costs_minor
        for column in RouteBaseline.__table__.columns:
            if column.name.endswith("_minor"):
                assert type(getattr(baseline, column.name)) is int
                assert (await session.execute(text(f"SELECT typeof({column.name}) FROM route_baselines"))).scalar_one() == "integer"


@pytest.mark.parametrize("model, identifier", [(Retailer, "synthetic-retailer-003"), (RouteBaseline, SEED_ROUTE_ID)])
async def test_seed_rejects_conflicting_provenance_without_relabelling(seeded, model, identifier):
    async with seeded.transaction() as session:
        record = await session.get(model, identifier)
        record.data_source = "conflicting-test-fixture"
    before = await snapshot(seeded)
    with pytest.raises(SeedConflictError, match="provenance"):
        await seed_synthetic(seeded)
    assert await snapshot(seeded) == before

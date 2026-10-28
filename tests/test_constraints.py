import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from app.models.db import Commitment, Mission, PurchaseEvent, RewardRelease


async def test_database_unique_constraints_present(database):
    async with database.engine.connect() as conn:
        for table, expected in {
            "purchase_events": {("idempotency_key",), ("mission_id", "retailer_id", "order_ref")},
            "reward_releases": {("idempotency_key",), ("mission_id",)},
            "commitments": {("retailer_id", "mission_id")},
        }.items():
            constraints = await conn.run_sync(lambda sync: inspect(sync).get_unique_constraints(table))
            actual = {tuple(item["column_names"]) for item in constraints}
            assert actual >= expected


async def test_duplicate_purchase_rejected_and_original_unchanged(participant, purchase_values):
    async with participant.transaction() as session:
        session.add(PurchaseEvent(**purchase_values()))
    with pytest.raises(IntegrityError, match="UNIQUE"):
        async with participant.transaction() as session:
            session.add(PurchaseEvent(**purchase_values(id="duplicate-purchase")))
    async with participant.transaction() as session:
        rows = (await session.scalars(select(PurchaseEvent))).all()
        assert len(rows) == 1
        assert rows[0].id == "simulated-purchase-001"


async def test_distinct_order_reference_allows_new_purchase(participant, purchase_values):
    async with participant.transaction() as session:
        session.add(PurchaseEvent(**purchase_values()))
        session.add(PurchaseEvent(**purchase_values(id="purchase-2", order_ref="ORDER-002", idempotency_key="synthetic-mission-001:synthetic-retailer-003:ORDER-002")))
    async with participant.transaction() as session:
        assert len((await session.scalars(select(PurchaseEvent))).all()) == 2


async def test_duplicate_release_rejected_and_original_unchanged(participant, release_values):
    async with participant.transaction() as session:
        session.add(RewardRelease(**release_values()))
    with pytest.raises(IntegrityError, match="UNIQUE"):
        async with participant.transaction() as session:
            session.add(RewardRelease(**release_values(id="duplicate-release")))
    async with participant.transaction() as session:
        assert len((await session.scalars(select(RewardRelease))).all()) == 1


async def test_duplicate_commitment_rejected(participant):
    async with participant.transaction() as session:
        original = await session.get(Commitment, "synthetic-commitment-001")
        row = {column.name: getattr(original, column.name) for column in Commitment.__table__.columns}
    with pytest.raises(IntegrityError, match="UNIQUE"):
        async with participant.transaction() as session:
            session.add(Commitment(**{**row, "id": "duplicate-commitment"}))


@pytest.mark.parametrize("changes", [
    {"idempotency_key": "arbitrary-key"},
    {"simulated_purchase": False},
    {"data_source": "verified"},
    {"quantity": 0},
    {"unit_cost_minor": -1},
    {"retailer_id": "synthetic-retailer-004", "idempotency_key": "synthetic-mission-001:synthetic-retailer-004:SYNTHETIC-ORDER-001"},
])
async def test_invalid_purchase_rejected_at_database(participant, purchase_values, changes):
    with pytest.raises(IntegrityError):
        async with participant.transaction() as session:
            session.add(PurchaseEvent(**purchase_values(**changes)))


@pytest.mark.parametrize("changes, expected", [
    ({"idempotency_key": "arbitrary-key"}, "key_format"),
    ({"data_source": "verified"}, "simulation_required"),
    ({"total_reward_minor": -1}, "exact_nonnegative"),
    ({"profitability_snapshot": {"nested": {"money": 0.1}}}, "exact integers"),
])
async def test_invalid_release_rejected_at_database(participant, release_values, changes, expected):
    with pytest.raises(IntegrityError, match=expected):
        async with participant.transaction() as session:
            session.add(RewardRelease(**release_values(**changes)))


async def test_money_orm_refuses_binary_float(participant, purchase_values):
    with pytest.raises(StatementError, match="Exact integer"):
        async with participant.transaction() as session:
            session.add(PurchaseEvent(**purchase_values(unit_price_minor=19.99)))


async def test_direct_sql_cannot_persist_fractional_money(participant, purchase_values):
    async with participant.transaction() as session:
        session.add(PurchaseEvent(**purchase_values()))
    with pytest.raises(IntegrityError, match="exact_nonnegative"):
        async with participant.transaction() as session:
            await session.execute(text("UPDATE purchase_events SET unit_price_minor=19.99"))


async def test_active_mission_requires_approval(participant):
    # ORM validation now rejects this before SQL; bypass it to retain DB coverage.
    with pytest.raises(IntegrityError, match="missions remediation validation failed"):
        async with participant.transaction() as session:
            await session.execute(text("UPDATE missions SET approved_by=NULL WHERE id='synthetic-mission-001'"))


async def test_commitment_cannot_use_another_retailers_observation(participant):
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        async with participant.transaction() as session:
            commitment = await session.get(Commitment, "synthetic-commitment-001")
            commitment.retailer_id = "synthetic-retailer-004"


async def test_direct_sql_cannot_persist_fractional_incentive_rate(participant):
    with pytest.raises(IntegrityError, match="exact_nonnegative"):
        async with participant.transaction() as session:
            await session.execute(text("UPDATE missions SET incentive_bps=500.5"))

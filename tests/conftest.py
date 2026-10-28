"""All databases and test artifacts remain inside the hackathon workspace."""

import httpx
import httpx2
import pytest
import pytest_asyncio

from app.config import Settings
from app.database import Database
from app.models.db import Commitment, Mission, Observation
from app.models.schemas.ai import AnalysisRequest
from app.services.seeding import SEED_ROUTE_ID, SEED_TIMESTAMP, seed_synthetic


def pytest_configure(config):
    # pytest creates basetemp itself, but its parent must exist on a fresh checkout.
    from app.config import WORKSPACE_ROOT

    (WORKSPACE_ROOT / ".test-artifacts").mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def no_http_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network HTTP transport forbidden in Milestone 1 tests")

    async def async_forbidden(*args, **kwargs):
        forbidden()

    for library in (httpx, httpx2):
        monkeypatch.setattr(library.HTTPTransport, "handle_request", forbidden)
        monkeypatch.setattr(library.AsyncHTTPTransport, "handle_async_request", async_forbidden)


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", use_mock_ai=True, _env_file=None)


@pytest_asyncio.fixture
async def database(settings):
    db = Database(settings)
    try:
        await db.create_schema()
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def seeded(database):
    await seed_synthetic(database)
    return database


@pytest.fixture
def analysis_request():
    return AnalysisRequest.model_validate({
        "route_baseline_id": SEED_ROUTE_ID,
        "observations": [{
            "id": "synthetic-observation-001", "retailer_id": "synthetic-retailer-003",
            "menu_path": ["2", "1"], "products": ["Synthetic Grain Pack"],
            "stated_barrier": "Synthetic delivery barrier",
        }],
    })


@pytest_asyncio.fixture
async def participant(seeded):
    async with seeded.transaction() as session:
        session.add(Observation(
            id="synthetic-observation-001", retailer_id="synthetic-retailer-003",
            session_id="synthetic-session-001", observed_at=SEED_TIMESTAMP,
            menu_path=["2", "1"], raw_session_log='["2","1"]', data_source="synthetic",
        ))
        session.add(Mission(
            id="synthetic-mission-001", route_baseline_id=SEED_ROUTE_ID,
            name="Synthetic test mission", qualifying_products=["Synthetic Grain Pack"],
            target=6, minimum_quantity=1,
            starts_at="2026-10-27T00:00:00.000000+00:00",
            ends_at="2026-11-04T00:00:00.000000+00:00",
            incentive_bps=500, margin_guardrail_bps=1000, status="ACTIVE",
            created_by="synthetic-manager", approved_by="synthetic-manager",
            approved_at=SEED_TIMESTAMP, parameters_hash="a" * 64, data_source="synthetic",
        ))
        await session.flush()
        session.add(Commitment(
            id="synthetic-commitment-001", retailer_id="synthetic-retailer-003",
            mission_id="synthetic-mission-001", route_baseline_id=SEED_ROUTE_ID,
            observation_id="synthetic-observation-001", product="Synthetic Grain Pack",
            quantity=10, data_source="synthetic",
        ))
    return seeded


@pytest.fixture
def purchase_values():
    def values(**overrides):
        row = {
            "id": "simulated-purchase-001", "commitment_id": "synthetic-commitment-001",
            "mission_id": "synthetic-mission-001", "retailer_id": "synthetic-retailer-003",
            "order_ref": "SYNTHETIC-ORDER-001",
            "idempotency_key": "synthetic-mission-001:synthetic-retailer-003:SYNTHETIC-ORDER-001",
            "product": "Synthetic Grain Pack", "quantity": 10, "unit_price_minor": 1999,
            "unit_cost_minor": 1200, "fulfillment_per_unit_minor": 50,
            "other_incremental_costs_minor": 100, "simulated_purchase": True,
            "purchased_at": SEED_TIMESTAMP, "recorded_at": SEED_TIMESTAMP,
            "recorded_by": "synthetic-manager", "data_source": "simulated",
        }
        row.update(overrides)
        return row
    return values


@pytest.fixture
def release_values():
    def values(**overrides):
        row = {
            "id": "simulated-release-001", "mission_id": "synthetic-mission-001",
            "idempotency_key": "synthetic-mission-001:release",
            "released_by": "synthetic-manager", "released_at": SEED_TIMESTAMP,
            "eligible_retailer_ids": ["synthetic-retailer-003"], "total_reward_minor": 1000,
            "profitability_snapshot": {"net_contribution_minor": 5000, "incentive_bps": 500},
            "data_source": "simulated",
        }
        row.update(overrides)
        return row
    return values

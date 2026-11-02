"""Exact financial decisions, ownership, replay and transaction safety."""
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.ai.client import MockAIClient
from app.models.db import AuditLog, Commitment, Mission, Observation, PurchaseEvent, RewardRelease, RouteBaseline
from app.models.schemas.demo import ReviewDecision
from app.models.schemas.workflow import ApproveMission, DraftMission, RecordCommitment, ReleaseReward, SimulatedPurchase
from app.services.demo import DemoError, DemoService
from app.services.mission_workflow import MissionWorkflow
from app.services.observations import observation_rows, seed_synthetic_observations
from app.services.seeding import SEED_ROUTE_ID
from app.services.stored_analysis import StoredAnalysisService

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
OWNER = 'synthetic-browser-a'


def action(cls, **values):
    return cls(request_id=uuid4(), demo_only=True, **values)


@pytest.fixture
async def planning(seeded):
    await seed_synthetic_observations(seeded)
    ids = [observation_rows()[i]['id'] for i in (0, 2)]
    saved = await StoredAnalysisService(seeded, MockAIClient()).analyze(SEED_ROUTE_ID, ids, 'workflow-test-analysis')
    await DemoService(seeded).review(OWNER, ReviewDecision(request_id=uuid4(), analysis_id=saved.provenance.id,
        strategy_index=0, decision='planning', demo_only=True))
    draft = action(DraftMission, analysis_id=saved.provenance.id, strategy_index=0,
        observation_ids=ids[:1], product='Synthetic Grain Pack', name='Synthetic trial', target=1,
        minimum_quantity=1, starts_at=(NOW - timedelta(days=1)).isoformat(),
        ends_at=(NOW + timedelta(days=7)).isoformat(), incentive_bps=500, margin_guardrail_bps=1000,
        reward_cap_minor=50000, minimum_contribution_minor=1)
    return MissionWorkflow(seeded, clock=lambda: NOW), draft


async def active(planning, **changes):
    service, draft = planning
    if changes:
        draft = DraftMission.model_validate({**draft.model_dump(), **changes})
    mission = await service.draft(OWNER, draft)
    approval = action(ApproveMission, human_approved=True)
    await service.approve(OWNER, mission['id'], approval)
    return service, draft, mission['id'], approval


async def purchased(planning, **changes):
    service, draft, mid, approval = await active(planning)
    commitment = await service.commit(OWNER, mid, action(RecordCommitment,
        observation_id=draft.observation_ids[0], quantity=10))
    purchase = action(SimulatedPurchase, commitment_id=commitment['id'], order_ref='SYNTHETIC-ORDER-1',
        product=draft.product, quantity=10, unit_price_minor=10001, unit_cost_minor=6000,
        fulfillment_per_unit_minor=501, other_incremental_costs_minor=999,
        purchased_at=NOW.isoformat(), simulated_purchase=True, **changes)
    result = await service.purchase(OWNER, mid, purchase)
    return service, mid, purchase, result, approval


async def count(database, model):
    async with database.transaction() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_explicit_approval_exact_profitability_reward_and_baseline_preserved(planning):
    service, draft = planning
    async with service.database.transaction() as session:
        baseline = await session.get(RouteBaseline, SEED_ROUTE_ID)
        before = {c.name: getattr(baseline, c.name) for c in RouteBaseline.__table__.columns}
    service, mid, purchase, result, approval = await purchased(planning)
    view = (await service.dashboard(OWNER))['missions'][0]
    assert view['qualified_retailer_ids'] == ['synthetic-retailer-003']
    assert view['unqualified_retailer_ids'] == [] and view['release_allowed']
    assert view['totals'] == {'revenue_minor': 100010, 'cogs_minor': 60000, 'fulfillment_minor': 5010,
        'operating_minor': 999, 'reward_minor': 5000, 'contribution_minor': 29001}
    assert (await service.dashboard(OWNER))['scenario']['reward_minor'] == 0
    release = action(ReleaseReward, human_approved=True)
    reward = await service.release(OWNER, mid, release)
    assert reward['total_reward_minor'] == 5000
    view = await service.dashboard(OWNER)
    assert view['missions'][0]['status'] == 'REWARD_RELEASED'
    assert view['scenario']['reward_minor'] == 5000 and view['scenario']['contribution_minor'] == 29001
    assert view['baseline_plus_simulation']['revenue_minor'] == before['monthly_revenue_minor'] + 100010
    async with service.database.transaction() as session:
        baseline = await session.get(RouteBaseline, SEED_ROUTE_ID)
        assert before == {c.name: getattr(baseline, c.name) for c in RouteBaseline.__table__.columns}
        stored = (await session.scalars(select(RewardRelease))).one()
        assert stored.data_source == 'simulated' and stored.profitability_snapshot['simulated_only']
    assert await service.purchase(OWNER, mid, purchase) == result
    assert await service.approve(OWNER, mid, approval) == {'id': mid, 'status': 'ACTIVE', 'demo_only': True}
    assert await service.release(OWNER, mid, release) == reward


@pytest.mark.parametrize('method', ['approve', 'commit', 'release'])
async def test_other_browser_cannot_mutate_or_enumerate_owned_missions(planning, method):
    service, draft = planning
    mid = (await service.draft(OWNER, draft))['id']
    args = {'approve': action(ApproveMission, human_approved=True),
            'commit': action(RecordCommitment, observation_id=draft.observation_ids[0], quantity=10),
            'release': action(ReleaseReward, human_approved=True)}
    before = await count(service.database, AuditLog)
    with pytest.raises(DemoError, match='not found'):
        await getattr(service, method)('other-browser', mid, args[method])
    assert await count(service.database, AuditLog) == before
    assert (await service.dashboard('other-browser'))['missions'] == []


async def test_review_and_explicit_approval_required_before_business_actions(planning):
    service, draft = planning
    with pytest.raises(DemoError, match='Accept'):
        await service.draft('other-browser', draft)
    mid = (await service.draft(OWNER, draft))['id']
    with pytest.raises(DemoError, match='active mission'):
        await service.commit(OWNER, mid, action(RecordCommitment, observation_id=draft.observation_ids[0], quantity=10))
    assert await count(service.database, Commitment) == 0
    with pytest.raises(ValidationError):
        action(ApproveMission, human_approved=False)


@pytest.mark.parametrize('change,reason', [
    ({'unit_cost_minor': 10001}, 'contribution'),
    ({'unit_cost_minor': 9400}, 'margin'),
])
async def test_nonprofitable_or_below_margin_reward_rolls_back(planning, change, reason):
    service, draft, mid, _ = await active(planning)
    commitment = await service.commit(OWNER, mid, action(RecordCommitment, observation_id=draft.observation_ids[0], quantity=10))
    data = dict(commitment_id=commitment['id'], order_ref='LOSS', product=draft.product, quantity=10,
        unit_price_minor=10001, unit_cost_minor=6000, fulfillment_per_unit_minor=0,
        other_incremental_costs_minor=0, purchased_at=NOW.isoformat(), simulated_purchase=True)
    await service.purchase(OWNER, mid, action(SimulatedPurchase, **{**data, **change}))
    before = await count(service.database, AuditLog)
    with pytest.raises(DemoError, match=reason):
        await service.release(OWNER, mid, action(ReleaseReward, human_approved=True))
    assert await count(service.database, AuditLog) == before
    assert await count(service.database, RewardRelease) == 0


async def test_reward_cap_and_distinct_retailer_target_are_enforced(planning):
    service, draft = planning
    ids = [observation_rows()[i]['id'] for i in (0, 2)]
    service, draft, mid, _ = await active(planning, target=2, observation_ids=ids, reward_cap_minor=1)
    commitment = await service.commit(OWNER, mid, action(RecordCommitment, observation_id=ids[0], quantity=10))
    await service.purchase(OWNER, mid, action(SimulatedPurchase, commitment_id=commitment['id'], order_ref='ONE',
        product=draft.product, quantity=10, unit_price_minor=10000, unit_cost_minor=1000,
        fulfillment_per_unit_minor=0, other_incremental_costs_minor=0, purchased_at=NOW.isoformat(), simulated_purchase=True))
    view = (await service.dashboard(OWNER))['missions'][0]
    assert len(view['qualified_retailer_ids']) == 1 and len(view['unqualified_retailer_ids']) == 1
    with pytest.raises(DemoError, match='target.*cap'):
        await service.release(OWNER, mid, action(ReleaseReward, human_approved=True))


async def test_concurrent_duplicate_purchases_and_releases_survive_service_restart(planning):
    service, mid, purchase, result, _ = await purchased(planning)
    duplicates = await asyncio.gather(*(service.purchase(OWNER, mid, purchase) for _ in range(3)))
    assert duplicates == [result] * 3
    assert await count(service.database, PurchaseEvent) == 1
    rewards = await asyncio.gather(*(service.release(OWNER, mid, action(ReleaseReward, human_approved=True)) for _ in range(3)))
    assert rewards[0] == rewards[1] == rewards[2]
    assert await count(service.database, RewardRelease) == 1
    restarted = MissionWorkflow(service.database, clock=lambda: NOW)
    assert await restarted.purchase(OWNER, mid, purchase) == result
    assert await restarted.release(OWNER, mid, action(ReleaseReward, human_approved=True)) == rewards[0]


async def test_same_order_with_new_request_replays_but_changed_values_conflict(planning):
    service, mid, purchase, result, _ = await purchased(planning)
    replay = SimulatedPurchase.model_validate({**purchase.model_dump(), 'request_id': uuid4()})
    assert await service.purchase(OWNER, mid, replay) == result
    changed = SimulatedPurchase.model_validate({**replay.model_dump(), 'request_id': uuid4(), 'unit_price_minor': 10002})
    with pytest.raises(DemoError, match='different simulated purchase'):
        await service.purchase(OWNER, mid, changed)
    assert await count(service.database, PurchaseEvent) == 1


async def test_competing_orders_cannot_exceed_committed_quantity(planning):
    service, mid, purchase, _, _ = await purchased(planning)
    orders = [SimulatedPurchase.model_validate({**purchase.model_dump(), 'request_id': uuid4(), 'order_ref': f'NEW-{i}'}) for i in range(2)]
    results = await asyncio.gather(*(service.purchase(OWNER, mid, p) for p in orders), return_exceptions=True)
    assert all(isinstance(r, DemoError) for r in results)
    assert await count(service.database, PurchaseEvent) == 1


@pytest.mark.parametrize('race', ['same_request', 'same_order', 'different_orders'])
async def test_concurrent_first_purchase_serializes_before_reading_balance(planning, race):
    service, draft, mid, _ = await active(planning)
    commitment = await service.commit(OWNER, mid, action(RecordCommitment,
        observation_id=draft.observation_ids[0], quantity=10))
    first = action(SimulatedPurchase, commitment_id=commitment['id'], order_ref='RACE-1',
        product=draft.product, quantity=10, unit_price_minor=10000, unit_cost_minor=6000,
        fulfillment_per_unit_minor=500, other_incremental_costs_minor=1000,
        purchased_at=NOW.isoformat(), simulated_purchase=True)
    second = first if race == 'same_request' else SimulatedPurchase.model_validate({
        **first.model_dump(), 'request_id': uuid4(),
        'order_ref': 'RACE-2' if race == 'different_orders' else first.order_ref,
    })
    results = await asyncio.gather(service.purchase(OWNER, mid, first),
                                   service.purchase(OWNER, mid, second), return_exceptions=True)
    assert await count(service.database, PurchaseEvent) == 1
    if race == 'different_orders':
        assert sum(isinstance(v, DemoError) for v in results) == 1
    else:
        assert results[0] == results[1] and not isinstance(results[0], Exception)


async def test_purchase_write_failure_rolls_back_receipt_and_commitment_status(planning):
    service, draft, mid, _ = await active(planning)
    commitment = await service.commit(OWNER, mid, action(RecordCommitment,
        observation_id=draft.observation_ids[0], quantity=10))
    data = action(SimulatedPurchase, commitment_id=commitment['id'], order_ref='ROLLBACK',
        product=draft.product, quantity=10, unit_price_minor=10000, unit_cost_minor=6000,
        fulfillment_per_unit_minor=500, other_incremental_costs_minor=1000,
        purchased_at=NOW.isoformat(), simulated_purchase=True)
    async with service.database.engine.begin() as connection:
        await connection.exec_driver_sql("CREATE TRIGGER fail_purchase BEFORE INSERT ON purchase_events BEGIN SELECT RAISE(ABORT, 'synthetic purchase failure'); END")
    before = await count(service.database, AuditLog)
    with pytest.raises(IntegrityError, match='synthetic purchase failure'):
        await service.purchase(OWNER, mid, data)
    assert await count(service.database, AuditLog) == before
    assert await count(service.database, PurchaseEvent) == 0
    async with service.database.transaction() as session:
        assert (await session.get(Commitment, commitment['id'])).status == 'PROVISIONAL_COMMITMENT'
    async with service.database.engine.begin() as connection:
        await connection.exec_driver_sql('DROP TRIGGER fail_purchase')
    assert (await service.purchase(OWNER, mid, data))['simulated_purchase']


async def test_order_splitting_does_not_change_retailer_reward_rounding(planning):
    service, draft, mid, _ = await active(planning)
    commitment = await service.commit(OWNER, mid, action(RecordCommitment, observation_id=draft.observation_ids[0], quantity=20))
    for i in range(2):
        await service.purchase(OWNER, mid, action(SimulatedPurchase, commitment_id=commitment['id'], order_ref=f'SPLIT-{i}',
            product=draft.product, quantity=10, unit_price_minor=1, unit_cost_minor=0, fulfillment_per_unit_minor=0,
            other_incremental_costs_minor=0, purchased_at=NOW.isoformat(), simulated_purchase=True))
    assert (await service.dashboard(OWNER))['missions'][0]['totals']['reward_minor'] == 1


async def test_release_failure_rolls_back_receipt_status_and_reward_then_retries(planning):
    service, mid, _, _, _ = await purchased(planning)
    release = action(ReleaseReward, human_approved=True)
    async with service.database.engine.begin() as connection:
        await connection.exec_driver_sql("CREATE TRIGGER fail_reward BEFORE INSERT ON reward_releases BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    before = await count(service.database, AuditLog)
    with pytest.raises(IntegrityError, match='synthetic failure'):
        await service.release(OWNER, mid, release)
    assert await count(service.database, AuditLog) == before
    assert await count(service.database, RewardRelease) == 0
    assert (await service.dashboard(OWNER))['missions'][0]['status'] == 'ACTIVE'
    async with service.database.engine.begin() as connection:
        await connection.exec_driver_sql('DROP TRIGGER fail_reward')
    assert (await service.release(OWNER, mid, release))['total_reward_minor'] == 5000


@pytest.mark.parametrize('fault', ['product', 'future_date', 'before_approval', 'expired', 'unknown_commitment'])
async def test_purchase_evidence_status_product_and_dates_are_checked(planning, fault):
    service, mid, purchase, _, _ = await purchased(planning)
    values = {**purchase.model_dump(), 'request_id': uuid4(), 'order_ref': 'INVALID'}
    if fault == 'product': values['product'] = 'Unsupported synthetic product'
    if fault == 'future_date': values['purchased_at'] = (NOW + timedelta(hours=1)).isoformat()
    if fault == 'before_approval': values['purchased_at'] = (NOW - timedelta(hours=1)).isoformat()
    if fault == 'unknown_commitment': values['commitment_id'] = 'missing'
    if fault == 'expired': service.clock = lambda: NOW + timedelta(days=10)
    with pytest.raises(DemoError):
        await service.purchase(OWNER, mid, SimulatedPurchase.model_validate(values))
    assert await count(service.database, PurchaseEvent) == 1


async def test_approved_parameter_or_source_evidence_tampering_fails_closed(planning):
    service, mid, _, _, _ = await purchased(planning)
    async with service.database.transaction() as session:
        mission = await session.get(Mission, mid)
        mission.incentive_bps = 1000
    with pytest.raises(DemoError, match='parameters changed'):
        await service.release(OWNER, mid, action(ReleaseReward, human_approved=True))


@pytest.mark.parametrize('value', [1.5, True, '1000', -1, 100000001])
def test_money_and_simulation_inputs_are_strict_bounded_integers(value):
    with pytest.raises(ValidationError):
        action(SimulatedPurchase, commitment_id='synthetic-commit', order_ref='SYNTHETIC',
            product='Synthetic Grain Pack', quantity=1, unit_price_minor=value, unit_cost_minor=0,
            fulfillment_per_unit_minor=0, other_incremental_costs_minor=0,
            purchased_at=NOW.isoformat(), simulated_purchase=True)

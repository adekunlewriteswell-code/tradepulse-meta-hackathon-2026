from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.db import AuditLog, Observation, Retailer, RouteBaseline
from app.models.schemas.demo import SessionAction, StartSession
from app.services.demo import DemoError, DemoService
from app.services.observations import load_stored_observations
from app.services.seeding import SEED_ROUTE_ID, synthetic_rows


def start_input(**kwargs):
    return StartSession(request_id=uuid4(), route_id=SEED_ROUTE_ID,
                        retailer_id='synthetic-retailer-003', **kwargs)


def action(state, digit=None, command='input', request_id=None):
    return SessionAction(request_id=request_id or uuid4(), expected_version=state['version'],
                         action=command, digit=digit)


async def progress(service, owner='browser-a'):
    state = await service.start(owner, start_input())
    for digit in ['1', '1', '2']:
        state = await service.act(owner, state['id'], action(state, digit))
    return state


async def counts(database):
    async with database.transaction() as session:
        return [(await session.scalar(select(func.count()).select_from(model)))
                for model in (AuditLog, Observation)]


async def test_numbered_state_transitions_invalid_back_cancel_and_restart(seeded):
    service = DemoService(seeded)
    state = await service.start('a', start_input())
    invalid = await service.act('a', state['id'], action(state, '9'))
    assert invalid['stage'] == 'HOME' and 'Invalid option' in invalid['message']
    assert invalid['version'] == 1
    state = await service.act('a', state['id'], action(invalid, '2'))
    assert state['stage'] == 'PRODUCT'
    state = await service.act('a', state['id'], action(state, '3'))
    assert state['stage'] == 'BARRIER'
    state = await service.act('a', state['id'], action(state, command='back'))
    assert state['stage'] == 'PRODUCT' and state['product'] is None
    state = await service.act('a', state['id'], action(state, '0'))
    assert state['stage'] == 'CANCELLED' and state['observation_id'] is None
    with pytest.raises(DemoError, match='ended'):
        await service.act('a', state['id'], action(state, '1'))
    restart = await service.start('a', start_input())
    assert restart['id'] != state['id'] and restart['stage'] == 'HOME'
    assert (await counts(seeded))[1] == 0


async def test_session_completion_preserves_raw_trace_and_repeated_submit_is_idempotent(seeded):
    service = DemoService(seeded)
    state = await progress(service)
    final = action(state, '1')
    completed = await service.act('browser-a', state['id'], final)
    before = await counts(seeded)
    assert await service.act('browser-a', state['id'], final) == completed
    assert await counts(seeded) == before
    assert completed['stage'] == 'COMPLETED' and before[1] == 1
    assert len(completed['inputs']) == 4 and completed['inputs'][-1]['digit'] == '1'
    assert 'owner' not in completed
    payload = await load_stored_observations(seeded, SEED_ROUTE_ID, [completed['observation_id']])
    assert payload.observations[0].menu_path == ['1', '1', '2', '1']
    assert payload.observations[0].stated_barrier.startswith('SYNTHETIC:')


async def test_start_replay_and_reused_request_conflict(seeded):
    service = DemoService(seeded)
    data = start_input()
    state = await service.start('a', data)
    before = await counts(seeded)
    assert await service.start('a', data) == state
    assert await counts(seeded) == before
    with pytest.raises(DemoError, match='different input'):
        await service.start('a', data.model_copy(update={'retailer_id': 'synthetic-retailer-004'}))
    assert await counts(seeded) == before


async def test_owner_stale_versions_and_unknown_sessions_fail_without_writes(seeded):
    service = DemoService(seeded)
    state = await service.start('a', start_input())
    before = await counts(seeded)
    with pytest.raises(DemoError, match='not found'):
        await service.get('b', state['id'])
    with pytest.raises(DemoError, match='not found'):
        await service.act('b', state['id'], action(state, '1'))
    with pytest.raises(DemoError, match='changed'):
        await service.act('a', state['id'], action(state, '1').model_copy(update={'expected_version': 3}))
    assert await counts(seeded) == before


async def test_expiration_and_input_limit_are_explicit(seeded):
    now = datetime(2026, 10, 29, tzinfo=UTC)
    service = DemoService(seeded, clock=lambda: now)
    state = await service.start('a', start_input())
    now += timedelta(minutes=31)
    assert (await service.get('a', state['id']))['stage'] == 'EXPIRED'
    state = await service.act('a', state['id'], action(state, '1'))
    assert state['stage'] == 'EXPIRED' and (await counts(seeded))[1] == 0
    state = await service.start('a', start_input())
    for _ in range(40):
        state = await service.act('a', state['id'], action(state, '9'))
    with pytest.raises(DemoError, match='input limit'):
        await service.act('a', state['id'], action(state, '1'))


async def test_cross_route_or_non_synthetic_identity_rolls_back_start(seeded):
    baseline = synthetic_rows()[0].copy()
    baseline.update(id='other-route', route_code='SYNTHETIC-OTHER')
    async with seeded.transaction() as session:
        session.add(RouteBaseline(**baseline))
        await session.flush()
        retailer = await session.get(Retailer, 'synthetic-retailer-003')
        retailer.route_baseline_id = 'other-route'
    before = await counts(seeded)
    with pytest.raises(DemoError, match='synthetic NGN'):
        await DemoService(seeded).start('a', start_input())
    assert await counts(seeded) == before


async def test_completion_rollback_then_same_key_retry(seeded):
    service = DemoService(seeded)
    state = await progress(service)
    final = action(state, '1')
    async with seeded.engine.begin() as connection:
        await connection.exec_driver_sql("CREATE TRIGGER test_fail_observation BEFORE INSERT ON observations BEGIN SELECT RAISE(ABORT, 'test storage failure'); END")
    before = await counts(seeded)
    with pytest.raises(IntegrityError, match='test storage failure'):
        await service.act('browser-a', state['id'], final)
    assert await counts(seeded) == before
    assert (await service.get('browser-a', state['id']))['stage'] == 'CONFIRM'
    async with seeded.engine.begin() as connection:
        await connection.exec_driver_sql('DROP TRIGGER test_fail_observation')
    assert (await service.act('browser-a', state['id'], final))['stage'] == 'COMPLETED'


async def test_concurrent_duplicate_and_competing_session_inputs(seeded):
    import asyncio

    service = DemoService(seeded)
    state = await service.start('a', start_input())
    command = action(state, '1')
    results = await asyncio.gather(*(service.act('a', state['id'], command) for _ in range(2)))
    assert results[0] == results[1]
    state = results[0]
    results = await asyncio.gather(service.act('a', state['id'], action(state, '1')),
                                   service.act('a', state['id'], action(state, '2')), return_exceptions=True)
    assert sum(isinstance(r, DemoError) for r in results) == 1
    assert (await service.get('a', state['id']))['version'] == 2

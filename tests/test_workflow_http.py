"""Browser boundaries, saved live attribution and complete simulated economics."""
from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.models.db import AIExtraction, Observation
from test_demo_http import client, complete, demo_app


def intelligence(client, mode='mock'):
    observation = complete(client)['observation_id']
    response = client.post('/demo/api/analyses', json={
        'request_id': str(uuid4()), 'observation_ids': [observation], 'mode': mode})
    assert response.status_code == 200
    return response.json(), observation


def draft_body(analysis, observation):
    now = datetime.now(UTC)
    return {'request_id': str(uuid4()), 'demo_only': True, 'analysis_id': analysis['id'],
        'strategy_index': 0, 'observation_ids': [observation], 'product': 'Synthetic Grain Pack',
        'name': 'Synthetic browser mission', 'target': 1, 'minimum_quantity': 10,
        'starts_at': (now - timedelta(minutes=1)).isoformat(), 'ends_at': (now + timedelta(days=7)).isoformat(),
        'incentive_bps': 500, 'margin_guardrail_bps': 1000,
        'reward_cap_minor': 50000, 'minimum_contribution_minor': 100}


def post(client, path, **fields):
    response = client.post('/demo/api/' + path, json={'request_id': str(uuid4()), 'demo_only': True, **fields})
    assert response.status_code == 200, response.text
    return response.json()


def test_complete_http_financial_workflow_restart_and_browser_ownership(client, demo_app):
    analysis, observation = intelligence(client)
    response = client.post('/demo/api/reviews', json={'request_id': str(uuid4()), 'analysis_id': analysis['id'],
        'strategy_index': 0, 'decision': 'planning', 'demo_only': True})
    assert response.status_code == 200
    draft = client.post('/demo/api/missions', json=draft_body(analysis, observation))
    assert draft.status_code == 200
    mid = draft.json()['id']
    path = 'missions/' + mid
    assert client.get('/demo/api/financials').json()['missions'][0]['status'] == 'DRAFT'
    post(client, path + '/approve', human_approved=True)
    commitment = post(client, path + '/commitments', observation_id=observation, quantity=10)
    post(client, path + '/purchases', commitment_id=commitment['id'], order_ref='SYNTHETIC-HTTP-ORDER',
        product='Synthetic Grain Pack', quantity=10, unit_price_minor=10000, unit_cost_minor=6000,
        fulfillment_per_unit_minor=500, other_incremental_costs_minor=1000,
        purchased_at=datetime.now(UTC).isoformat(), simulated_purchase=True)
    release = {'request_id': str(uuid4()), 'demo_only': True, 'human_approved': True}
    released = client.post('/demo/api/' + path + '/release', json=release)
    assert released.status_code == 200 and released.json()['total_reward_minor'] == 5000
    view = client.get('/demo/api/financials').json()
    assert view['scenario']['contribution_minor'] == 29000
    assert view['baseline']['total_retailers'] == 10 and view['baseline']['active_retailers'] == 2
    assert view['missions'][0]['reward_released']
    cookie = client.cookies.get('tp_local_demo')
    # A new application/DB engine exercises persisted capabilities and operation receipts.
    with TestClient(create_app(demo_app.state.settings), base_url='http://127.0.0.1', client=('127.0.0.1', 1)) as restarted:
        restarted.cookies.set('tp_local_demo', cookie, path='/demo')
        token = restarted.get('/demo/api/bootstrap').json()['csrf_token']
        restarted.headers.update({'Origin': 'http://127.0.0.1', 'X-Demo-CSRF': token})
        assert restarted.post('/demo/api/' + path + '/release', json=release).json() == released.json()
        assert restarted.get('/demo/api/financials').json() == view
        restarted.cookies.clear()
        token = restarted.get('/demo/api/bootstrap').json()['csrf_token']
        restarted.headers['X-Demo-CSRF'] = token
        assert restarted.get('/demo/api/financials').json()['missions'] == []
        assert restarted.post('/demo/api/' + path + '/release', json=release).status_code == 404


@pytest.mark.parametrize('path', ['missions', 'missions/missing/approve', 'missions/missing/commitments',
                                 'missions/missing/purchases', 'missions/missing/release'])
def test_every_new_write_requires_browser_csrf_and_same_origin(client, path):
    assert client.post('/demo/api/' + path, json={}, headers={'X-Demo-CSRF': 'wrong'}).status_code == 403
    assert client.post('/demo/api/' + path, json={}, headers={'Origin': 'https://evil.example'}).status_code == 403


def test_financial_read_requires_a_browser_capability(demo_app):
    with TestClient(demo_app, base_url='http://localhost', client=('127.0.0.1', 1)) as guest:
        assert guest.get('/demo/api/financials').status_code == 403


def test_saved_live_view_revalidates_and_never_exposes_private_traces(client, demo_app):
    # All content is generated locally; no genuine provider response is included in tests.
    analysis, observation = intelligence(client)

    async def mark_synthetic_fixture_as_live():
        async with demo_app.state.database.transaction() as session:
            row = await session.get(AIExtraction, analysis['id'])
            row.data_source = 'meta_live'
            row.model_name = 'muse-spark-1.3'
            row.call_details = {'inference_mode': 'live', 'http_status': 200,
                'private_trace': 'DO-NOT-EXPOSE-PRIVATE-PROVIDER-TRACE',
                'raw_provider_response': json.dumps({'model': row.model_name, 'choices': [{
                    'finish_reason': 'stop', 'message': {'content': row.raw_response}}]})}
    client.portal.call(mark_synthetic_fixture_as_live)
    saved = client.get('/demo/api/analyses/' + analysis['id'])
    assert saved.status_code == 200
    assert saved.json()['mode'] == 'live' and saved.json()['provider'] == 'Meta Model API'
    assert saved.json()['model'] == 'muse-spark-1.3'
    assert saved.json()['output'] is not None
    for private in ('DO-NOT-EXPOSE', 'call_details', 'raw_provider_response', 'raw_request', 'authorization'):
        assert private not in saved.text
    assert any(v['id'] == analysis['id'] and v['source'] == 'meta_live'
               for v in client.get('/demo/api/overview').json()['analyses'])
    assert client.post('/demo/api/reviews', json={'request_id': str(uuid4()), 'analysis_id': analysis['id'],
        'strategy_index': 0, 'decision': 'planning', 'demo_only': True}).status_code == 200
    assert client.post('/demo/api/missions', json=draft_body(analysis, observation)).status_code == 200

    async def change_evidence():
        async with demo_app.state.database.transaction() as session:
            row = await session.get(Observation, observation)
            raw = json.loads(row.raw_session_log)
            raw['stated_barrier'] = 'SYNTHETIC: a different statement.'
            row.raw_session_log = json.dumps(raw)
    client.portal.call(change_evidence)
    assert client.get('/demo/api/analyses/' + analysis['id']).status_code == 422
    assert client.post('/demo/api/reviews', json={'request_id': str(uuid4()), 'analysis_id': analysis['id'],
        'strategy_index': 0, 'decision': 'planning', 'demo_only': True}).status_code == 422


def test_live_inference_and_missing_human_or_simulation_acknowledgments_fail(client):
    assert client.post('/demo/api/analyses', json={'request_id': str(uuid4()),
        'observation_ids': ['synthetic-observation'], 'mode': 'live'}).status_code == 422
    for suffix in ('approve', 'release'):
        assert client.post('/demo/api/missions/missing/' + suffix, json={
            'request_id': str(uuid4()), 'demo_only': True, 'human_approved': False}).status_code == 422

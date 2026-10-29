"""Explicitly gated local browser demonstration, not a production management API."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from pydantic import ValidationError

from app.ai.meta import strict_json
from app.demo_security import COOKIE, csrf
from app.domain.money import format_ngn
from app.models.db import AIExtraction, AuditLog, Observation, Retailer, RouteBaseline
from app.models.schemas.ai import AnalysisResponse
from app.models.schemas.demo import AnalyzeSelection, ReviewDecision, SessionAction, StartSession
from app.services.demo import DemoError, DemoService, audit_values, digest
from app.services.observations import ObservationDataError, StoredSession, load_stored_observations
from app.services.seeding import SEED_ROUTE_ID
from app.services.stored_analysis import AnalysisConflictError, AnalysisInProgressError, StoredAnalysisService

router = APIRouter(prefix='/demo', include_in_schema=False)
WEB = Path(__file__).resolve().parents[1] / 'web'


async def browser_record(request, required=True):
    token = request.cookies.get(COOKIE, '')
    if re.fullmatch('[0-9a-f]{64}', token):
        async with request.app.state.database.transaction() as session:
            row = await session.get(AuditLog, 'browser-' + digest(token)[:48])
            if (row is not None and row.event == 'DEMO_BROWSER' and row.data_source == 'synthetic'
                    and row.details.get('digest') == digest(token)
                    and datetime.fromisoformat(row.details['expires_at']) > datetime.now(UTC)):
                return token
    if required:
        raise HTTPException(403, 'Reload the local demo to establish a browser session.')
    return None


async def browser(request: Request):
    return digest(await browser_record(request))


@router.get('')
@router.get('/')
async def page():
    return FileResponse(WEB / 'index.html')


@router.get('/assets/{name}')
async def asset(name: str):
    if name not in ('demo.css', 'demo.js'):
        raise HTTPException(404, 'Asset not found')
    return FileResponse(WEB / name, media_type='text/css' if name.endswith('.css') else 'text/javascript')


@router.get('/api/bootstrap')
async def bootstrap(request: Request, response: Response):
    token = await browser_record(request, required=False)
    if token is None:
        token = secrets.token_hex(32)
        identity = 'browser-' + digest(token)[:48]
        async with request.app.state.database.transaction() as session:
            session.add(AuditLog(**audit_values(identity, 'DEMO_BROWSER', None,
                {'digest': digest(token), 'expires_at': (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
                 'demo_only': True})))
        response.set_cookie(COOKIE, token, max_age=86400, httponly=True, samesite='strict', path='/demo')
    return {'csrf_token': csrf(token), 'demo_only': True, 'route_id': SEED_ROUTE_ID,
            'default_mode': request.app.state.settings.demo_default_mode,
            'notice': 'Synthetic demonstration. Local browser capability is not manager authentication.'}


@router.post('/api/sessions')
async def start(data: StartSession, request: Request, owner=Depends(browser)):
    return await DemoService(request.app.state.database).start(owner, data)


@router.get('/api/sessions/{session_id}')
async def get_session(session_id: UUID, request: Request, owner=Depends(browser)):
    return await DemoService(request.app.state.database).get(owner, str(session_id))


@router.post('/api/sessions/{session_id}/input')
async def input_session(session_id: UUID, data: SessionAction, request: Request, owner=Depends(browser)):
    return await DemoService(request.app.state.database).act(owner, str(session_id), data)


@router.get('/api/overview')
async def overview(request: Request, owner=Depends(browser)):
    async with request.app.state.database.transaction() as session:
        route = await session.get(RouteBaseline, SEED_ROUTE_ID)
        retailers = (await session.scalars(select(Retailer).where(
            Retailer.route_baseline_id == SEED_ROUTE_ID).order_by(Retailer.code))).all()
        observations = (await session.scalars(select(Observation).join(Retailer).where(
            Retailer.route_baseline_id == SEED_ROUTE_ID, Observation.data_source == 'synthetic'
        ).order_by(Observation.created_at.desc(), Observation.id.desc()).limit(100))).all()
        analyses = (await session.scalars(select(AIExtraction).where(
            AIExtraction.route_baseline_id == SEED_ROUTE_ID,
            AIExtraction.data_source.in_(['mock', 'meta_simulated'])
        ).order_by(AIExtraction.created_at.desc()).limit(20))).all()
        evidence = []
        for row in observations:
            item = {'id': row.id, 'retailer_id': row.retailer_id, 'observed_at': row.observed_at,
                    'session_id': row.session_id, 'products': [], 'barrier': '', 'category': 'Unsupported session',
                    'analyzable': False, 'raw_session_log': row.raw_session_log[:4096]}
            try:
                if len(row.raw_session_log.encode('utf-8')) > 4096:
                    raise ValueError('Oversized stored session')
                raw = StoredSession.model_validate(strict_json(row.raw_session_log))
                item.update(products=raw.products, barrier=raw.stated_barrier,
                            category='Retailer-stated barrier', analyzable=raw.menu_path == row.menu_path)
                from app.services.demo import BARRIERS
                item['category'] = next((label for label, text in BARRIERS.values()
                                         if text == raw.stated_barrier), 'Retailer-stated barrier')
            except (ValueError, TypeError):
                item['barrier'] = 'Legacy or invalid session content requires explicit review.'
            evidence.append(item)
    if route is None or route.currency != 'NGN' or route.data_source != 'synthetic':
        raise DemoError('Synthetic NGN baseline is unavailable.', 503)
    return {'demo_only': True, 'route': {'id': route.id, 'name': route.name,
                'total': route.total_retailers, 'baseline_active': route.active_retailers,
                'baseline_inactive': route.total_retailers - route.active_retailers,
                'revenue': format_ngn(route.monthly_revenue_minor),
                'contribution': format_ngn(route.monthly_contribution_minor)},
            'retailers': [{'id': row.id, 'name': row.name, 'status': row.status} for row in retailers],
            'observations': evidence,
            'analyses': [{'id': row.id, 'status': row.validation_status, 'source': row.data_source,
                          'created_at': row.created_at} for row in analyses]}


async def analysis_view(database, row_id):
    async with database.transaction() as session:
        row = await session.get(AIExtraction, row_id)
        if (row is None or row.route_baseline_id != SEED_ROUTE_ID
                or row.data_source not in ('mock', 'meta_simulated')):
            raise DemoError('Saved offline analysis not found.', 404)
        reviews = (await session.scalars(select(AuditLog).where(
            AuditLog.event == 'DEMO_RECOMMENDATION_REVIEW', AuditLog.source_event_id == row.id
        ).order_by(AuditLog.created_at.desc()).limit(100))).all()
    output = None
    if row.validation_status == 'EXTRACTION_VALID' and row.parsed_output:
        payload = await load_stored_observations(database, SEED_ROUTE_ID, row.observation_ids)
        try:
            output = AnalysisResponse.model_validate(row.parsed_output, context={
                'observation_ids': row.observation_ids, 'products': {p for v in payload.observations for p in v.products},
                'barriers': {v.id: v.stated_barrier for v in payload.observations}}).model_dump(mode='json')
        except ValidationError:
            raise DemoError('Saved analysis failed current evidence validation; no result is displayed.', 422) from None
    return {'id': row.id, 'mode': 'mock' if row.data_source == 'mock' else 'simulated',
            'model': row.model_name, 'status': row.validation_status, 'run_state': row.run_state,
            'created_at': row.created_at, 'observation_ids': row.observation_ids, 'output': output,
            'error_category': (row.call_details or {}).get('error_category'),
            'reviews': [{'id': v.id, 'strategy_index': v.details['strategy_index'],
                         'decision': v.details['decision'], 'demo_only': True, 'created_at': v.created_at}
                        for v in reviews], 'demo_only': True}


@router.post('/api/analyses')
async def analyze(data: AnalyzeSelection, request: Request, owner=Depends(browser)):
    state = request.app.state
    client = state.ai_client if data.mode == 'mock' else state.demo_simulated_client
    if getattr(client, 'data_source', None) != ('mock' if data.mode == 'mock' else 'meta_simulated'):
        raise DemoError('The browser demonstration never invokes live inference.', 403)
    key = 'demo:' + owner[:16] + ':' + str(data.request_id)
    try:
        result = await StoredAnalysisService(state.database, client).analyze(
            SEED_ROUTE_ID, data.observation_ids, key)
    except (ObservationDataError, AnalysisConflictError) as exc:
        raise DemoError(str(exc), 422) from None
    except AnalysisInProgressError as exc:
        raise DemoError(str(exc), 409) from None
    return await analysis_view(state.database, result.provenance.id)


@router.get('/api/analyses/{analysis_id}')
async def get_analysis(analysis_id: str, request: Request, owner=Depends(browser)):
    if len(analysis_id) > 64:
        raise DemoError('Invalid analysis identifier.', 422)
    return await analysis_view(request.app.state.database, analysis_id)


@router.get('/api/evidence/{observation_id}/trace')
async def trace(observation_id: str, request: Request, owner=Depends(browser)):
    await load_stored_observations(request.app.state.database, SEED_ROUTE_ID, [observation_id])
    async with request.app.state.database.transaction() as session:
        observation = await session.get(Observation, observation_id)
        row = (await session.scalars(select(AuditLog).where(
            AuditLog.event == 'DEMO_USSD_STATE', AuditLog.source_event_id == observation.session_id
        ).order_by(AuditLog.details['version'].as_integer().desc()).limit(1))).first()
    return {'observation_id': observation_id, 'inputs': row.details['inputs'] if row else [],
            'notice': 'Synthetic numbered-menu inputs. Observation is not a purchase.'}


@router.post('/api/reviews')
async def review(data: ReviewDecision, request: Request, owner=Depends(browser)):
    return await DemoService(request.app.state.database).review(owner, data)

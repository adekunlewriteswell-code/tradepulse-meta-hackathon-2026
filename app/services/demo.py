"""Local demo sessions and reviews, stored as explicit append-only audit events."""
from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from pydantic import ValidationError

from app.models.db import AIExtraction, AuditLog, Observation, Retailer, RouteBaseline
from app.models.db.base import utc_timestamp
from app.models.schemas.ai import AnalysisResponse
from app.services.observations import OBSERVATION_CONTRACT, StoredSession
from app.services.seeding import SEED_ROUTE_ID

PRODUCTS = {'1': ['Synthetic Grain Pack'], '2': ['Synthetic Soap Carton'],
            '3': ['Synthetic Grain Pack', 'Synthetic Soap Carton']}
BARRIERS = {
    '1': ('Minimum order', 'SYNTHETIC: minimum order quantity exceeds available shelf space.'),
    '2': ('Delivery reliability', 'SYNTHETIC: previous delivery dates were unreliable.'),
    '3': ('Working capital', 'SYNTHETIC: limited working capital prevents stocking more cartons.'),
    '4': ('Product availability', 'SYNTHETIC: requested product availability is inconsistent.'),
    '5': ('Sales velocity', 'SYNTHETIC: low sales velocity leaves stock unsold.'),
    '9': ('Other / unspecified', 'SYNTHETIC: another barrier was selected; details were not supplied.'),
}
MENUS = {
    'HOME': ('Welcome to TradePulse', [('1', 'Report buying interest'), ('2', 'Report a buying barrier')]),
    'PRODUCT': ('Which product interests you?', [('1', 'Grain Pack'), ('2', 'Soap Carton'), ('3', 'Both products')]),
    'BARRIER': ('What is holding you back?', [(k, v[0]) for k, v in BARRIERS.items()]),
    'CONFIRM': ('Save this synthetic observation?', [('1', 'Confirm and save')]),
    'COMPLETED': ('Observation saved', []), 'CANCELLED': ('Session cancelled', []),
    'EXPIRED': ('Session expired', []),
}


class DemoError(ValueError):
    def __init__(self, message, status=409):
        self.status = status
        super().__init__(message)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def event_id(prefix, value):
    return prefix + str(uuid5(NAMESPACE_URL, value))


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':')))


def audit_values(identity, event, source, details):
    return dict(id=identity, event=event, source_event_id=source, actor='local-demo',
                created_at=utc_timestamp(), data_source='synthetic', details=details)


async def claim(session, identity, event, source, details):
    try:
        # The audit replacement guard intentionally rejects duplicate PK inserts,
        # including ON CONFLICT DO NOTHING. Preserve it and isolate replay failure.
        async with session.begin_nested():
            await session.execute(insert(AuditLog).values(**audit_values(identity, event, source, details)))
        return True
    except IntegrityError:
        existing = await session.get(AuditLog, identity)
        if existing is None:
            raise
        if (existing.event != event or existing.source_event_id != source
                or existing.details != details or existing.data_source != 'synthetic'):
            raise DemoError('Request key was already used for different input.')
        return False


class DemoService:
    def __init__(self, database, clock=None):
        self.database = database
        self.clock = clock or (lambda: datetime.now(UTC))

    async def retailer(self, session, route_id, retailer_id):
        route = await session.get(RouteBaseline, route_id)
        retailer = await session.get(Retailer, retailer_id)
        if (route_id != SEED_ROUTE_ID or route is None or route.currency != 'NGN'
                or route.data_source != 'synthetic' or retailer is None
                or retailer.route_baseline_id != route_id or retailer.data_source != 'synthetic'):
            raise DemoError('Select a retailer on the synthetic NGN demonstration route.', 422)
        return retailer

    async def latest(self, session, owner, session_id):
        row = (await session.scalars(select(AuditLog).where(
            AuditLog.event == 'DEMO_USSD_STATE', AuditLog.source_event_id == session_id
        ).order_by(AuditLog.details['version'].as_integer().desc()).limit(1))).first()
        if row is None or row.details['owner'] != owner:
            raise DemoError('Session not found for this local browser.', 404)
        return dict(row.details)

    def expired(self, state):
        return self.clock() - datetime.fromisoformat(state['started_at']) > timedelta(minutes=30)

    def view(self, state):
        state = dict(state)
        if state['stage'] not in ('COMPLETED', 'CANCELLED', 'EXPIRED') and self.expired(state):
            state.update(stage='EXPIRED', message='This session expired. Restart to begin again.')
        state.pop('owner', None)
        title, options = MENUS[state['stage']]
        state.update(title=title, options=[{'digit': k, 'label': v} for k, v in options],
                     terminal=state['stage'] in ('COMPLETED', 'CANCELLED', 'EXPIRED'))
        return state

    async def get(self, owner, session_id):
        async with self.database.transaction() as session:
            return self.view(await self.latest(session, owner, session_id))

    async def start(self, owner, data):
        body = data.model_dump(mode='json')
        operation_id = event_id('demo-op-', owner + str(data.request_id))
        session_id = str(uuid5(NAMESPACE_URL, 'ussd:' + owner + str(data.request_id)))
        async with self.database.transaction() as session:
            fresh = await claim(session, operation_id, 'DEMO_USSD_OPERATION', session_id,
                                {'owner': owner, 'fingerprint': fingerprint(body)})
            if not fresh:
                return self.view(await self.latest(session, owner, session_id))
            retailer = await self.retailer(session, data.route_id, data.retailer_id)
            state = dict(id=session_id, owner=owner, route_id=data.route_id, retailer_id=retailer.id,
                         retailer_name=retailer.name, version=0, stage='HOME', home=None, product=None,
                         barrier=None, observation_id=None, started_at=self.clock().isoformat(),
                         inputs=[], message='Choose a numbered option. This is synthetic data only.')
            session.add(AuditLog(**audit_values(event_id('demo-state-', operation_id),
                        'DEMO_USSD_STATE', session_id, state)))
        return self.view(state)

    async def act(self, owner, session_id, data):
        body = {'session_id': session_id, **data.model_dump(mode='json')}
        operation_id = event_id('demo-op-', owner + str(data.request_id))
        state_id = event_id('demo-state-', operation_id)
        async with self.database.transaction() as session:
            fresh = await claim(session, operation_id, 'DEMO_USSD_OPERATION', session_id,
                                {'owner': owner, 'fingerprint': fingerprint(body)})
            if not fresh:
                row = await session.get(AuditLog, state_id)
                if row is None:
                    raise DemoError('Replay could not be reconciled. Reload the saved session.')
                return self.view(row.details)
            state = await self.latest(session, owner, session_id)
            if state['version'] != data.expected_version:
                raise DemoError('Session changed. Reload before sending another option.')
            if state['stage'] in ('COMPLETED', 'CANCELLED', 'EXPIRED'):
                raise DemoError('This session has ended. Restart to begin a new session.')
            if len(state['inputs']) >= 40:
                raise DemoError('Session input limit reached. Restart to begin again.')
            await self.retailer(session, state['route_id'], state['retailer_id'])
            state['inputs'] = [*state['inputs'], {'action': data.action, 'digit': data.digit}]
            state['version'] += 1
            state['message'] = 'Choose the next numbered option.'
            if self.expired(state):
                state.update(stage='EXPIRED', message='Session expired. No observation was saved.')
            elif data.action == 'cancel' or (data.action == 'input' and data.digit == '0'):
                state.update(stage='CANCELLED', message='Cancelled. No observation was saved.')
            elif data.action == 'back':
                previous = {'HOME': 'HOME', 'PRODUCT': 'HOME', 'BARRIER': 'PRODUCT', 'CONFIRM': 'BARRIER'}
                state['stage'] = previous[state['stage']]
                for field in {'HOME': ['home', 'product', 'barrier'], 'PRODUCT': ['product', 'barrier'],
                              'BARRIER': ['barrier']}[state['stage']]:
                    state[field] = None
            else:
                digit, stage = data.digit, state['stage']
                if digit not in dict(MENUS[stage][1]):
                    state['message'] = 'Invalid option. Select a number shown in this menu.'
                elif stage == 'HOME':
                    state.update(home=digit, stage='PRODUCT')
                elif stage == 'PRODUCT':
                    state.update(product=digit, stage='BARRIER')
                elif stage == 'BARRIER':
                    state.update(barrier=digit, stage='CONFIRM', message=BARRIERS[digit][1])
                elif stage == 'CONFIRM':
                    observation_id = 'ussd-' + session_id
                    raw = StoredSession(schema_version=OBSERVATION_CONTRACT, data_source='synthetic',
                        menu_path=[state['home'], state['product'], state['barrier'], digit],
                        products=PRODUCTS[state['product']], stated_barrier=BARRIERS[state['barrier']][1])
                    session.add(Observation(id=observation_id, retailer_id=state['retailer_id'],
                        session_id=session_id, observed_at=utc_timestamp(), data_source='synthetic',
                        menu_path=raw.menu_path, raw_session_log=raw.model_dump_json()))
                    state.update(stage='COMPLETED', observation_id=observation_id,
                                 message='Saved as an observation, not a purchase or verified demand.')
            session.add(AuditLog(**audit_values(state_id, 'DEMO_USSD_STATE', session_id, state)))
        return self.view(state)

    async def review(self, owner, data):
        identity = event_id('demo-review-', owner + str(data.request_id))
        details = {**data.model_dump(mode='json'), 'owner': owner, 'not_mission_approval': True}
        async with self.database.transaction() as session:
            await claim(session, identity, 'DEMO_RECOMMENDATION_REVIEW', data.analysis_id, details)
            row = await session.get(AIExtraction, data.analysis_id)
            if (row is None or row.route_baseline_id != SEED_ROUTE_ID or row.run_state != 'DONE'
                    or row.validation_status != 'EXTRACTION_VALID' or not row.parsed_output
                    or row.data_source not in ('mock', 'meta_simulated')):
                raise DemoError('Review requires a valid saved offline recommendation.', 422)
            try:
                output = AnalysisResponse.model_validate(row.parsed_output,
                    context={'observation_ids': row.observation_ids})
            except ValidationError:
                raise DemoError('Saved recommendation is invalid; no review was recorded.', 422) from None
            if data.strategy_index >= len(output.activation_strategies):
                raise DemoError('Select a strategy present in the saved recommendation.', 422)
        return {'id': identity, 'decision': data.decision, 'demo_only': True, 'not_mission_approval': True}

"""Local synthetic missions. Deterministic financial decisions never depend on AI."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.domain.money import MAX_MINOR_UNITS
from app.models.db import AIExtraction, AuditLog, Commitment, Mission, PurchaseEvent, RewardRelease, RouteBaseline
from app.services.demo import DemoError, audit_values, claim, event_id, fingerprint
from app.services.saved_intelligence import validated_saved_output
from app.services.seeding import SEED_ROUTE_ID


def money_totals(purchases, mission):
    revenue = sum(p.quantity * p.unit_price_minor for p in purchases)
    cogs = sum(p.quantity * p.unit_cost_minor for p in purchases)
    fulfillment = sum(p.quantity * p.fulfillment_per_unit_minor for p in purchases)
    operating = sum(p.other_incremental_costs_minor for p in purchases)
    # One floor per retailer, computed from aggregate revenue, independent of order splitting.
    retailer_revenue = {}
    for p in purchases:
        retailer_revenue[p.retailer_id] = retailer_revenue.get(p.retailer_id, 0) + p.quantity * p.unit_price_minor
    reward = sum(value * mission.incentive_bps // 10000 for value in retailer_revenue.values())
    contribution = revenue - cogs - fulfillment - operating - reward
    result = dict(revenue_minor=revenue, cogs_minor=cogs, fulfillment_minor=fulfillment,
                  operating_minor=operating, reward_minor=reward, contribution_minor=contribution)
    if any(abs(value) > MAX_MINOR_UNITS for value in result.values()):
        raise DemoError('Scenario exceeds exact accounting bounds.', 422)
    return result


class MissionWorkflow:
    def __init__(self, database, clock=None):
        self.database = database
        self.clock = clock or (lambda: datetime.now(UTC))

    def now(self):
        return self.clock().astimezone(UTC).isoformat(timespec='microseconds')

    async def operate(self, owner, action, mission_id, data, apply):
        identity = event_id('mission-op-', owner + str(data.request_id))
        details = {'owner': owner, 'action': action, 'input': data.model_dump(mode='json'), 'demo_only': True}
        async with self.database.transaction() as session:
            fresh = await claim(session, identity, 'DEMO_MISSION_OPERATION', mission_id, details)
            if not fresh:
                saved = await session.get(AuditLog, identity + '-result')
                if saved is None:
                    raise DemoError('Operation is incomplete; reconcile the saved request.')
                return saved.details['result']
            result = await apply(session)
            await session.flush()
            session.add(AuditLog(**audit_values(identity + '-result', 'DEMO_MISSION_RESULT', mission_id,
                {'owner': owner, 'action': action, 'result': result, 'demo_only': True})))
        return result

    async def owned(self, session, owner, mission_id):
        mission = await session.get(Mission, mission_id)
        link = await session.get(AuditLog, 'plan-' + mission_id)
        if (mission is None or link is None or link.event != 'DEMO_MISSION_PLAN'
                or link.details.get('owner') != owner or mission.data_source != 'synthetic'
                or mission.route_baseline_id != SEED_ROUTE_ID):
            raise DemoError('Owned demonstration mission not found.', 404)
        params = link.details
        actual = {key: getattr(mission, key) for key in params['mission_parameters']}
        if actual != params['mission_parameters'] or (mission.status != 'DRAFT'
                and mission.parameters_hash != fingerprint(params['mission_parameters'])):
            raise DemoError('Mission parameters changed after planning or approval.', 422)
        return mission, params

    async def evidence(self, session, params):
        row = await session.get(AIExtraction, params['analysis_id'])
        if row is None:
            raise DemoError('Saved recommendation is unavailable.', 422)
        try:
            payload, output = await validated_saved_output(self.database, row)
        except ValueError:
            raise DemoError('Saved recommendation or evidence changed; mission action rejected.', 422) from None
        strategy = output.activation_strategies[params['strategy_index']]
        selected = [v for v in payload.observations if v.id in params['observation_ids']]
        if (len(selected) != len(params['observation_ids']) or any(
                v.id not in strategy.evidence_basis or params['product'] not in v.products for v in selected)):
            raise DemoError('Mission evidence does not support the selected product and strategy.', 422)
        return selected

    def window(self, mission):
        if not mission.starts_at <= self.now() <= mission.ends_at:
            raise DemoError('Mission is outside its approved time window.', 422)

    async def draft(self, owner, data):
        mission_id = event_id('m4-', owner + str(data.request_id))

        async def apply(session):
            plans = await session.scalar(select(func.count()).select_from(AuditLog).where(
                AuditLog.event == 'DEMO_MISSION_PLAN', AuditLog.details['owner'].as_string() == owner))
            if plans >= 50:
                raise DemoError('This bounded demonstration supports at most 50 missions per browser.', 422)
            row = await session.get(AIExtraction, data.analysis_id)
            review = (await session.scalars(select(AuditLog).where(
                AuditLog.event == 'DEMO_RECOMMENDATION_REVIEW', AuditLog.source_event_id == data.analysis_id,
                AuditLog.details['owner'].as_string() == owner,
                AuditLog.details['strategy_index'].as_integer() == data.strategy_index,
            ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(1))).first()
            if row is None or review is None or review.details.get('decision') != 'planning':
                raise DemoError('Accept this saved strategy for planning before drafting a mission.', 422)
            params = {**data.model_dump(mode='json'), 'owner': owner, 'review_id': review.id}
            try:
                selected = await self.evidence(session, params)
            except (IndexError, KeyError):
                raise DemoError('Select a strategy present in the saved recommendation.', 422) from None
            if len({v.retailer_id for v in selected}) < data.target:
                raise DemoError('Target exceeds the number of distinct evidenced retailers.', 422)
            if not (data.starts_at < data.ends_at and data.ends_at > self.now()
                    and datetime.fromisoformat(data.ends_at) - datetime.fromisoformat(data.starts_at) <= timedelta(days=30)):
                raise DemoError('Use a future-ending mission window of at most 30 days.', 422)
            fields = dict(route_baseline_id=SEED_ROUTE_ID, name=data.name,
                qualifying_products=[data.product], target=data.target, minimum_quantity=data.minimum_quantity,
                starts_at=data.starts_at, ends_at=data.ends_at, incentive_bps=data.incentive_bps,
                margin_guardrail_bps=data.margin_guardrail_bps)
            mission = Mission(id=mission_id, **fields, created_by='local-demo-manager',
                              status='DRAFT', data_source='synthetic')
            session.add(mission)
            params['mission_parameters'] = fields
            session.add(AuditLog(**audit_values('plan-' + mission_id, 'DEMO_MISSION_PLAN', mission_id, params)))
            await session.flush()
            return {'id': mission_id, 'status': 'DRAFT', 'demo_only': True}
        return await self.operate(owner, 'draft', mission_id, data, apply)

    async def approve(self, owner, mission_id, data):
        async def apply(session):
            mission, params = await self.owned(session, owner, mission_id)
            await self.evidence(session, params)
            if mission.status != 'DRAFT' or self.now() > mission.ends_at:
                raise DemoError('Only an unexpired draft can be explicitly approved.')
            mission.approved_by = 'local-demo-manager'
            mission.approved_at = self.now()
            mission.parameters_hash = fingerprint(params['mission_parameters'])
            mission.status = 'ACTIVE'
            return {'id': mission_id, 'status': 'ACTIVE', 'demo_only': True}
        return await self.operate(owner, 'approve', mission_id, data, apply)

    async def commit(self, owner, mission_id, data):
        async def apply(session):
            mission, params = await self.owned(session, owner, mission_id)
            selected = await self.evidence(session, params)
            self.window(mission)
            observation = next((v for v in selected if v.id == data.observation_id), None)
            if mission.status != 'ACTIVE' or observation is None or data.quantity < mission.minimum_quantity:
                raise DemoError('Commitment requires an active mission, qualifying evidence and minimum quantity.', 422)
            identity = event_id('commit-', mission_id + observation.retailer_id)
            existing = await session.get(Commitment, identity)
            if existing:
                if existing.observation_id != observation.id or existing.quantity != data.quantity:
                    raise DemoError('This retailer already has a different mission commitment.')
                return {'id': identity, 'retailer_id': observation.retailer_id, 'demo_only': True}
            session.add(Commitment(id=identity, retailer_id=observation.retailer_id, mission_id=mission_id,
                route_baseline_id=SEED_ROUTE_ID, observation_id=observation.id, product=params['product'],
                quantity=data.quantity, status='PROVISIONAL_COMMITMENT', data_source='synthetic'))
            return {'id': identity, 'retailer_id': observation.retailer_id, 'demo_only': True}
        return await self.operate(owner, 'commit', mission_id, data, apply)

    async def purchase(self, owner, mission_id, data):
        async def apply(session):
            mission, params = await self.owned(session, owner, mission_id)
            selected = await self.evidence(session, params)
            self.window(mission)
            commitment = await session.get(Commitment, data.commitment_id)
            if (mission.status != 'ACTIVE' or commitment is None or commitment.mission_id != mission_id
                    or commitment.status not in ('PROVISIONAL_COMMITMENT', 'SIMULATED_PURCHASE')
                    or commitment.observation_id not in [v.id for v in selected]
                    or data.product != commitment.product or data.quantity < mission.minimum_quantity
                    or data.quantity > commitment.quantity or not mission.starts_at <= data.purchased_at <= mission.ends_at
                    or data.purchased_at > self.now() or data.purchased_at < mission.approved_at):
                raise DemoError('Purchase must match an active approved mission, commitment, product, quantity and dates.', 422)
            key = mission_id + ':' + commitment.retailer_id + ':' + data.order_ref
            existing = (await session.scalars(select(PurchaseEvent).where(PurchaseEvent.idempotency_key == key))).first()
            fields = {key: value for key, value in data.model_dump().items()
                      if key not in ('request_id', 'demo_only')}
            if existing:
                if any(getattr(existing, key) != value for key, value in fields.items()):
                    raise DemoError('Order reference already records a different simulated purchase.')
                return {'id': existing.id, 'simulated_purchase': True}
            previous = (await session.scalars(select(PurchaseEvent).where(
                PurchaseEvent.commitment_id == commitment.id))).all()
            if sum(v.quantity for v in previous) + data.quantity > commitment.quantity:
                raise DemoError('Cumulative purchases exceed the committed quantity.', 422)
            identity = event_id('purchase-', key)
            session.add(PurchaseEvent(id=identity, **fields, mission_id=mission_id,
                retailer_id=commitment.retailer_id, idempotency_key=key,
                recorded_at=self.now(), recorded_by='local-demo-manager', data_source='simulated'))
            commitment.status = 'SIMULATED_PURCHASE'
            return {'id': identity, 'simulated_purchase': True}
        return await self.operate(owner, 'purchase', mission_id, data, apply)

    async def calculation(self, session, mission, params):
        selected = await self.evidence(session, params)
        commitments = (await session.scalars(select(Commitment).where(Commitment.mission_id == mission.id))).all()
        purchases = (await session.scalars(select(PurchaseEvent).where(PurchaseEvent.mission_id == mission.id))).all()
        qualified, unqualified = [], []
        selected_by_retailer = {v.retailer_id: v for v in selected}
        valid_purchases = []
        for retailer_id, observation in selected_by_retailer.items():
            commitment = next((v for v in commitments if v.retailer_id == retailer_id), None)
            rows = [v for v in purchases if v.retailer_id == retailer_id]
            valid = bool(commitment and rows and commitment.observation_id == observation.id
                and commitment.product in mission.qualifying_products
                and commitment.status in ('PROVISIONAL_COMMITMENT', 'SIMULATED_PURCHASE', 'REWARD_ELIGIBLE', 'REWARD_RELEASED')
                and commitment.quantity >= mission.minimum_quantity
                and sum(v.quantity for v in rows) <= commitment.quantity and all(
                    v.commitment_id == commitment.id and v.product == commitment.product
                    and v.simulated_purchase and v.data_source == 'simulated'
                    and mission.starts_at <= v.purchased_at <= mission.ends_at
                    and mission.approved_at <= v.purchased_at <= self.now()
                    and v.quantity >= mission.minimum_quantity for v in rows))
            (qualified if valid else unqualified).append(retailer_id)
            if valid:
                valid_purchases.extend(rows)
        totals = money_totals(purchases, mission)
        all_purchases_qualify = len(valid_purchases) == len(purchases) and len(commitments) <= len(selected)
        eligible_totals = money_totals(valid_purchases, mission)
        reasons = []
        if mission.status not in ('ACTIVE', 'TARGET_MET', 'REWARD_RELEASED') or not mission.approved_by:
            reasons.append('Explicit human approval is required.')
        if not mission.starts_at <= self.now() <= mission.ends_at:
            reasons.append('Mission is outside its approved time window.')
        if len(qualified) < mission.target:
            reasons.append('Distinct qualified-retailer target is not met.')
        if not all_purchases_qualify:
            reasons.append('One or more purchase records fail deterministic eligibility checks.')
        if eligible_totals['reward_minor'] > params['reward_cap_minor']:
            reasons.append('Reward exceeds the approved integer-kobo cap.')
        if eligible_totals['contribution_minor'] < params['minimum_contribution_minor']:
            reasons.append('Minimum positive contribution is not met.')
        if eligible_totals['revenue_minor'] <= 0 or (eligible_totals['contribution_minor'] * 10000
                < eligible_totals['revenue_minor'] * mission.margin_guardrail_bps):
            reasons.append('Contribution margin guardrail is not met.')
        return {'totals': totals, 'qualified_retailer_ids': qualified, 'unqualified_retailer_ids': unqualified,
                'release_allowed': not reasons, 'reasons': reasons, 'rounding': 'Floor kobo once per retailer',
                'simulated_only': True}

    async def release(self, owner, mission_id, data):
        async def apply(session):
            mission, params = await self.owned(session, owner, mission_id)
            existing = (await session.scalars(select(RewardRelease).where(RewardRelease.mission_id == mission_id))).first()
            if existing:
                return {'id': existing.id, 'total_reward_minor': existing.total_reward_minor, 'simulated_only': True}
            result = await self.calculation(session, mission, params)
            if not result['release_allowed']:
                raise DemoError('Simulated reward blocked: ' + ' '.join(result['reasons']), 422)
            identity = event_id('reward-', mission_id)
            mission.status = 'REWARD_RELEASED'
            for commitment in (await session.scalars(select(Commitment).where(
                    Commitment.mission_id == mission_id, Commitment.retailer_id.in_(result['qualified_retailer_ids'])))).all():
                commitment.status = 'REWARD_RELEASED'
            session.add(RewardRelease(id=identity, mission_id=mission_id, idempotency_key=mission_id + ':release',
                released_by='local-demo-manager', released_at=self.now(), data_source='simulated',
                eligible_retailer_ids=result['qualified_retailer_ids'], total_reward_minor=result['totals']['reward_minor'],
                profitability_snapshot=result))
            return {'id': identity, 'total_reward_minor': result['totals']['reward_minor'], 'simulated_only': True}
        return await self.operate(owner, 'release', mission_id, data, apply)

    async def dashboard(self, owner):
        async with self.database.transaction() as session:
            route = await session.get(RouteBaseline, SEED_ROUTE_ID)
            links = (await session.scalars(select(AuditLog).where(AuditLog.event == 'DEMO_MISSION_PLAN',
                AuditLog.details['owner'].as_string() == owner).order_by(AuditLog.created_at.desc()).limit(50))).all()
            missions = []
            total = {key: 0 for key in ('revenue_minor', 'cogs_minor', 'fulfillment_minor', 'operating_minor', 'reward_minor', 'contribution_minor')}
            for link in links:
                mission, params = await self.owned(session, owner, link.source_event_id)
                result = await self.calculation(session, mission, params)
                commitments = (await session.scalars(select(Commitment).where(Commitment.mission_id == mission.id))).all()
                purchases = (await session.scalars(select(PurchaseEvent).where(PurchaseEvent.mission_id == mission.id))).all()
                released = (await session.scalars(select(RewardRelease).where(RewardRelease.mission_id == mission.id))).first()
                released_reward = released.total_reward_minor if released else 0
                # Only released reward is booked as expense; proposed reward remains visible separately.
                scenario = {**result['totals'], 'reward_minor': released_reward,
                    'contribution_minor': result['totals']['contribution_minor'] + result['totals']['reward_minor'] - released_reward}
                for key in total:
                    total[key] += scenario[key]
                missions.append({'id': mission.id, 'name': mission.name, 'status': mission.status,
                    'approved_by': mission.approved_by, 'approved_at': mission.approved_at,
                    'starts_at': mission.starts_at, 'ends_at': mission.ends_at,
                    'product': params['product'], 'observation_ids': params['observation_ids'],
                    'analysis_id': params['analysis_id'], 'strategy_index': params['strategy_index'],
                    'target': mission.target, 'minimum_quantity': mission.minimum_quantity,
                    'incentive_bps': mission.incentive_bps, 'margin_guardrail_bps': mission.margin_guardrail_bps,
                    'reward_cap_minor': params['reward_cap_minor'], 'minimum_contribution_minor': params['minimum_contribution_minor'],
                    'commitments': [{'id': v.id, 'retailer_id': v.retailer_id, 'quantity': v.quantity,
                                     'observation_id': v.observation_id, 'status': v.status} for v in commitments],
                    'purchases': [{'id': v.id, 'retailer_id': v.retailer_id, 'quantity': v.quantity,
                                   'order_ref': v.order_ref, 'simulated_purchase': True} for v in purchases],
                    'reward_released_minor': released_reward, 'reward_released': released is not None,
                    **result})
            baseline = {'total_retailers': route.total_retailers, 'active_retailers': route.active_retailers,
                'inactive_retailers': route.total_retailers - route.active_retailers,
                'revenue_minor': route.monthly_revenue_minor, 'contribution_minor': route.monthly_contribution_minor}
            combined = {'revenue_minor': baseline['revenue_minor'] + total['revenue_minor'],
                        'contribution_minor': baseline['contribution_minor'] + total['contribution_minor']}
            return {'baseline': baseline, 'scenario': total, 'baseline_plus_simulation': combined,
                    'missions': missions, 'demo_only': True, 'notice': 'Synthetic scenario; no causal proof or real growth.'}

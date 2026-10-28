"""Foundation schema only: business actions and reward calculation are deferred."""

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, ForeignKeyConstraint, JSON, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.models.db.base import Base, ExactInteger, Record, nonnegative_integer
from app.models.db.constraints import baseline_balance_sql, mission_metadata_sql
from app.models.schemas.mission import MissionMetadata


class RouteBaseline(Record, Base):
    __tablename__ = "route_baselines"
    __table_args__ = (
        CheckConstraint("total_retailers > 0 AND active_retailers BETWEEN 0 AND total_retailers", name="retailer_counts"),
        CheckConstraint("period_days > 0", name="period_positive"),
        *(nonnegative_integer(c) for c in ("period_days", "total_retailers", "active_retailers")),
        CheckConstraint("length(currency) = 3", name="currency_code"),
        *(nonnegative_integer(c) for c in (
            "monthly_revenue_minor", "monthly_cogs_minor", "monthly_fulfillment_minor",
            "monthly_incentive_minor", "monthly_other_costs_minor")),
        CheckConstraint("typeof(monthly_contribution_minor) = 'integer'", name="contribution_exact"),
        CheckConstraint(baseline_balance_sql(), name="contribution_consistent"),
    )
    route_code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    currency: Mapped[str] = mapped_column(String(3))
    period_start: Mapped[str] = mapped_column(String(10))
    period_days: Mapped[int] = mapped_column(ExactInteger)
    total_retailers: Mapped[int] = mapped_column(ExactInteger)
    active_retailers: Mapped[int] = mapped_column(ExactInteger)
    monthly_revenue_minor: Mapped[int] = mapped_column(ExactInteger)
    monthly_cogs_minor: Mapped[int] = mapped_column(ExactInteger)
    monthly_fulfillment_minor: Mapped[int] = mapped_column(ExactInteger)
    monthly_incentive_minor: Mapped[int] = mapped_column(ExactInteger)
    monthly_other_costs_minor: Mapped[int] = mapped_column(ExactInteger)
    monthly_contribution_minor: Mapped[int] = mapped_column(ExactInteger)


class Retailer(Record, Base):
    __tablename__ = "retailers"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="status_valid"),
        UniqueConstraint("id", "route_baseline_id"),
    )
    route_baseline_id: Mapped[str] = mapped_column(ForeignKey("route_baselines.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16))


class Observation(Record, Base):
    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("id", "retailer_id"),)
    retailer_id: Mapped[str] = mapped_column(ForeignKey("retailers.id", ondelete="RESTRICT"))
    session_id: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[str] = mapped_column(String(40))
    menu_path: Mapped[list[str]] = mapped_column(JSON)
    raw_session_log: Mapped[str] = mapped_column(Text)


class Mission(Record, Base):
    __tablename__ = "missions"
    __table_args__ = (
        UniqueConstraint("id", "route_baseline_id"),
        CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'TARGET_MET', 'REWARD_RELEASED', 'EXPIRED')", name="status_valid"),
        CheckConstraint("target > 0 AND minimum_quantity > 0", name="positive_targets"),
        *(nonnegative_integer(c) for c in ("target", "minimum_quantity", "incentive_bps", "margin_guardrail_bps")),
        CheckConstraint("incentive_bps BETWEEN 0 AND 10000 AND margin_guardrail_bps BETWEEN 0 AND 10000", name="rates_valid"),
        CheckConstraint("ends_at > starts_at", name="window_order"),
        CheckConstraint("status IN ('DRAFT', 'EXPIRED') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL AND parameters_hash IS NOT NULL)", name="approval_required"),
        # Escape literal colons so SQLAlchemy does not parse timestamp text as binds.
        CheckConstraint(mission_metadata_sql().replace(':', r'\:'), name="metadata_valid"),
    )
    route_baseline_id: Mapped[str] = mapped_column(ForeignKey("route_baselines.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(160))
    qualifying_products: Mapped[list[str]] = mapped_column(JSON)
    target: Mapped[int] = mapped_column(ExactInteger)
    minimum_quantity: Mapped[int] = mapped_column(ExactInteger)
    starts_at: Mapped[str] = mapped_column(String(40))
    ends_at: Mapped[str] = mapped_column(String(40))
    incentive_bps: Mapped[int] = mapped_column(ExactInteger)
    margin_guardrail_bps: Mapped[int] = mapped_column(ExactInteger)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT")
    created_by: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[str | None] = mapped_column(String(40))
    parameters_hash: Mapped[str | None] = mapped_column(String(64))


class Commitment(Record, Base):
    __tablename__ = "commitments"
    __table_args__ = (
        UniqueConstraint("retailer_id", "mission_id"),
        UniqueConstraint("id", "mission_id", "retailer_id"),
        ForeignKeyConstraint(["retailer_id", "route_baseline_id"], ["retailers.id", "retailers.route_baseline_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["mission_id", "route_baseline_id"], ["missions.id", "missions.route_baseline_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["observation_id", "retailer_id"], ["observations.id", "observations.retailer_id"], ondelete="RESTRICT"),
        CheckConstraint("status IN ('PROVISIONAL_COMMITMENT', 'SIMULATED_PURCHASE', 'REWARD_ELIGIBLE', 'REWARD_RELEASED', 'EXPIRED')", name="status_valid"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        nonnegative_integer("quantity"),
    )
    retailer_id: Mapped[str] = mapped_column(String(64))
    mission_id: Mapped[str] = mapped_column(String(64))
    route_baseline_id: Mapped[str] = mapped_column(String(64))
    observation_id: Mapped[str] = mapped_column(String(64))
    product: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[int] = mapped_column(ExactInteger)
    status: Mapped[str] = mapped_column(String(32), default="PROVISIONAL_COMMITMENT")


class PurchaseEvent(Record, Base):
    __tablename__ = "purchase_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        UniqueConstraint("mission_id", "retailer_id", "order_ref"),
        ForeignKeyConstraint(["commitment_id", "mission_id", "retailer_id"], ["commitments.id", "commitments.mission_id", "commitments.retailer_id"], ondelete="RESTRICT"),
        CheckConstraint("idempotency_key = mission_id || ':' || retailer_id || ':' || order_ref", name="key_format"),
        CheckConstraint("length(order_ref) > 0 AND instr(order_ref, ':') = 0", name="order_ref_valid"),
        CheckConstraint("simulated_purchase = 1 AND data_source = 'simulated'", name="simulation_required"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        nonnegative_integer("quantity"),
        *(nonnegative_integer(c) for c in (
            "unit_price_minor", "unit_cost_minor", "fulfillment_per_unit_minor", "other_incremental_costs_minor")),
    )
    commitment_id: Mapped[str] = mapped_column(String(64))
    mission_id: Mapped[str] = mapped_column(String(64))
    retailer_id: Mapped[str] = mapped_column(String(64))
    order_ref: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(240))
    product: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[int] = mapped_column(ExactInteger)
    unit_price_minor: Mapped[int] = mapped_column(ExactInteger)
    unit_cost_minor: Mapped[int] = mapped_column(ExactInteger)
    fulfillment_per_unit_minor: Mapped[int] = mapped_column(ExactInteger)
    other_incremental_costs_minor: Mapped[int] = mapped_column(ExactInteger)
    simulated_purchase: Mapped[bool] = mapped_column(Boolean, default=True)
    purchased_at: Mapped[str] = mapped_column(String(40))
    recorded_at: Mapped[str] = mapped_column(String(40))
    recorded_by: Mapped[str] = mapped_column(String(64))


class RewardRelease(Record, Base):
    __tablename__ = "reward_releases"
    __table_args__ = (
        UniqueConstraint("mission_id"),
        UniqueConstraint("idempotency_key"),
        # char(58) avoids SQLAlchemy text() interpreting ':release' as a bind parameter.
        CheckConstraint("idempotency_key = mission_id || char(58) || 'release'", name="key_format"),
        CheckConstraint("data_source = 'simulated'", name="simulation_required"),
        nonnegative_integer("total_reward_minor"),
    )
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    released_by: Mapped[str] = mapped_column(String(64))
    released_at: Mapped[str] = mapped_column(String(40))
    eligible_retailer_ids: Mapped[list[str]] = mapped_column(JSON)
    total_reward_minor: Mapped[int] = mapped_column(ExactInteger)
    # Snapshots store monetary values as integer *_minor fields, rates as integer *_bps.
    profitability_snapshot: Mapped[dict] = mapped_column(JSON)


class AuditLog(Record, Base):
    __tablename__ = "audit_logs"
    __table_args__ = {'sqlite_with_rowid': False}
    event: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    mission_id: Mapped[str | None] = mapped_column(ForeignKey("missions.id", ondelete="RESTRICT"))
    source_event_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(240))
    details: Mapped[dict] = mapped_column(JSON)


class AIExtraction(Record, Base):
    __tablename__ = "ai_extractions"
    __table_args__ = (
        CheckConstraint("parse_status IN ('NOT_ATTEMPTED', 'PARSED', 'FAILED')", name="parse_status_valid"),
        CheckConstraint("validation_status IN ('UNEXTRACTED', 'EXTRACTION_VALID', 'EXTRACTION_FAILED', 'EXTRACTION_INVALID')", name="validation_status_valid"),
    )
    route_baseline_id: Mapped[str] = mapped_column(ForeignKey("route_baselines.id", ondelete="RESTRICT"))
    observation_ids: Mapped[list[str]] = mapped_column(JSON)
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    raw_request: Mapped[str] = mapped_column(Text)
    raw_response: Mapped[str | None] = mapped_column(Text)
    parsed_output: Mapped[dict | None] = mapped_column(JSON)
    parse_status: Mapped[str] = mapped_column(String(24))
    validation_status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)


@event.listens_for(Mission, 'before_insert')
@event.listens_for(Mission, 'before_update')
def validate_mission_metadata(mapper, connection, target):
    fields = {name: getattr(target, name) for name in MissionMetadata.model_fields}
    fields['status'] = target.status if target.status is not None else 'DRAFT'
    validated = MissionMetadata.model_validate(fields)
    for name in ('starts_at', 'ends_at', 'approved_at'):
        setattr(target, name, getattr(validated, name))

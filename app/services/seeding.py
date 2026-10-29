"""Repeatable insert-only seeding: existing records are never overwritten."""

from decimal import Decimal

from sqlalchemy.dialects.sqlite import insert

from app.database import Database
from app.domain.money import to_minor_units
from app.models.db import Retailer, RouteBaseline

SEED_TIMESTAMP = "2026-10-27T00:00:00.000000+00:00"
SEED_ROUTE_ID = "synthetic-route-001"


class SeedConflictError(ValueError):
    """A reserved synthetic identifier is occupied by a different record."""


def synthetic_rows() -> tuple[dict, list[dict]]:
    revenue = Decimal("2000000.00")
    cogs = Decimal("1400000.00")
    fulfillment = Decimal("100000.00")
    incentive = Decimal("0.00")
    other = Decimal("50000.00")
    baseline = {
        "id": SEED_ROUTE_ID, "route_code": "SYNTHETIC-ROUTE-001",
        "name": "Synthetic Demonstration Route", "currency": "NGN",
        "created_at": SEED_TIMESTAMP, "data_source": "synthetic",
        "period_start": "2026-10-01", "period_days": 31,
        "total_retailers": 10, "active_retailers": 2,
        "monthly_revenue_minor": to_minor_units(revenue),
        "monthly_cogs_minor": to_minor_units(cogs),
        "monthly_fulfillment_minor": to_minor_units(fulfillment),
        "monthly_incentive_minor": to_minor_units(incentive),
        "monthly_other_costs_minor": to_minor_units(other),
        "monthly_contribution_minor": to_minor_units(revenue - cogs - fulfillment - incentive - other),
    }
    retailers = [{
        "id": f"synthetic-retailer-{number:03d}", "route_baseline_id": SEED_ROUTE_ID,
        "code": f"SYNTHETIC-RETAILER-{number:03d}",
        "name": f"Synthetic Retailer {number:02d}",
        "status": "ACTIVE" if number <= 2 else "INACTIVE",
        "created_at": SEED_TIMESTAMP, "data_source": "synthetic",
    } for number in range(1, 11)]
    return baseline, retailers


async def seed_synthetic(database: Database) -> None:
    baseline, retailers = synthetic_rows()
    async with database.transaction() as session:
        existing_route = await session.get(RouteBaseline, baseline["id"])
        if existing_route is not None and (
            existing_route.data_source != "synthetic" or existing_route.route_code != baseline["route_code"]
        ):
            raise SeedConflictError("Synthetic route identifier conflicts with existing provenance")
        if existing_route is not None and existing_route.currency != "NGN":
            raise SeedConflictError("Legacy currency requires the explicit backed-up currency correction")
        await session.execute(insert(RouteBaseline).values(**baseline).on_conflict_do_nothing(index_elements=["id"]))
        for retailer in retailers:
            existing = await session.get(Retailer, retailer["id"])
            if existing is not None and (
                existing.data_source != "synthetic" or existing.code != retailer["code"]
                or existing.route_baseline_id != SEED_ROUTE_ID
            ):
                raise SeedConflictError("Synthetic retailer identifier conflicts with existing provenance")
            await session.execute(insert(Retailer).values(**retailer).on_conflict_do_nothing(index_elements=["id"]))

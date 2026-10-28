"""Run from workspace root: python -m data.seed_synthetic."""

import asyncio
import sys
from pathlib import Path

# Also support the exact direct-script command documented in the review.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.database import Database
from app.services.seeding import seed_synthetic
from app.models.db import Retailer, RouteBaseline
from sqlalchemy import func, select


async def main() -> None:
    database = Database(Settings())
    try:
        await database.create_schema()
        await seed_synthetic(database)
        async with database.transaction() as session:
            counts = dict((await session.execute(
                select(Retailer.status, func.count()).where(Retailer.data_source == "synthetic").group_by(Retailer.status)
            )).all())
            routes = (await session.execute(
                select(func.count()).select_from(RouteBaseline).where(RouteBaseline.data_source == "synthetic")
            )).scalar_one()
        print(f"Synthetic seed applied (insert-only): {sum(counts.values())} synthetic retailers "
              f"({counts.get('ACTIVE', 0)} active, {counts.get('INACTIVE', 0)} inactive), {routes} synthetic route baseline(s).")
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())

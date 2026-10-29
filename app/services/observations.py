"""Synthetic raw-session representation and trusted persisted-observation loading."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select

from app.ai.meta import strict_json
from app.database import Database
from app.models.db import Observation, Retailer, RouteBaseline
from app.models.schemas.ai import AnalysisRequest
from app.services.seeding import SEED_ROUTE_ID, SEED_TIMESTAMP

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Product = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
MenuDigit = Annotated[str, StringConstraints(pattern=r"^[0-9]$")]
OBSERVATION_CONTRACT = "synthetic-observation-v1"


class StoredSession(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    schema_version: Literal["synthetic-observation-v1"]
    data_source: Literal["synthetic"]
    menu_path: list[MenuDigit] = Field(min_length=1, max_length=8)
    products: list[Product] = Field(min_length=1, max_length=5)
    stated_barrier: Text


class ObservationDataError(ValueError):
    pass


def observation_rows() -> list[dict]:
    barriers = [
        "SYNTHETIC: minimum order quantity exceeds available shelf space.",
        "SYNTHETIC: previous delivery dates were unreliable.",
        "SYNTHETIC: limited working capital prevents stocking more cartons.",
        "SYNTHETIC: requested product availability is inconsistent.",
        "SYNTHETIC: low sales velocity leaves stock unsold.",
    ]
    rows = []
    for number in range(3, 11):
        menu = ["2", "1", str(number - 3)]
        raw = StoredSession(
            schema_version=OBSERVATION_CONTRACT, data_source="synthetic", menu_path=menu,
            products=["Synthetic Grain Pack" if number % 2 else "Synthetic Soap Carton"],
            stated_barrier=barriers[(number - 3) % len(barriers)],
        )
        rows.append({
            "id": f"synthetic-m2-observation-{number:03d}",
            "retailer_id": f"synthetic-retailer-{number:03d}",
            "session_id": f"synthetic-m2-session-{number:03d}", "observed_at": SEED_TIMESTAMP,
            "created_at": SEED_TIMESTAMP, "menu_path": menu,
            "raw_session_log": raw.model_dump_json(), "data_source": "synthetic",
        })
    return rows


async def seed_synthetic_observations(database: Database) -> list[str]:
    rows = observation_rows()
    async with database.transaction() as session:
        for row in rows:
            retailer = await session.get(Retailer, row["retailer_id"])
            if (retailer is None or retailer.route_baseline_id != SEED_ROUTE_ID
                    or retailer.data_source != "synthetic" or retailer.status != "INACTIVE"):
                raise ObservationDataError("Synthetic inactive retailer fixture required")
            existing = await session.get(Observation, row["id"])
            if existing is not None:
                if any(getattr(existing, name) != value for name, value in row.items()):
                    raise ObservationDataError("Reserved observation conflicts with stored content")
            else:
                session.add(Observation(**row))
    return [row["id"] for row in rows]


async def load_stored_observations(database: Database, route_id: str, ids: list[str]) -> AnalysisRequest:
    if (not 1 <= len(ids) <= 8 or len(set(ids)) != len(ids)
            or any(not isinstance(v, str) or not v.strip() or len(v) > 64 for v in ids)
            or not isinstance(route_id, str) or not 1 <= len(route_id) <= 64):
        raise ObservationDataError("Select 1-8 unique bounded observation IDs")
    inputs = []
    async with database.transaction() as session:
        route = await session.get(RouteBaseline, route_id)
        if route is None or route.data_source != "synthetic" or route.currency != "NGN":
            raise ObservationDataError("A synthetic NGN route is required")
        pairs = (await session.execute(select(Observation, Retailer).join(
            Retailer, Observation.retailer_id == Retailer.id).where(Observation.id.in_(ids)))).all()
        if len(pairs) != len(ids):
            raise ObservationDataError("Unknown observation selection")
        for observation, retailer in sorted(pairs, key=lambda pair: pair[0].id):
            if (retailer.route_baseline_id != route_id or retailer.data_source != "synthetic"
                    or observation.data_source != "synthetic"):
                raise ObservationDataError("Observation does not belong to this synthetic route")
            if len(observation.raw_session_log.encode("utf-8")) > 4096:
                raise ObservationDataError("Stored raw session exceeds its size limit")
            try:
                raw = StoredSession.model_validate(strict_json(observation.raw_session_log))
                if raw.menu_path != observation.menu_path:
                    raise ValueError("Menu-path mismatch")
            except (ValueError, TypeError):
                raise ObservationDataError("Malformed or incompatible stored session content") from None
            inputs.append({
                "id": observation.id, "retailer_id": retailer.id, "menu_path": raw.menu_path,
                "products": raw.products, "stated_barrier": raw.stated_barrier,
            })
    return AnalysisRequest.model_validate({"route_baseline_id": route_id, "observations": inputs})


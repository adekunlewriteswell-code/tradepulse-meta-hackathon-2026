"""Persisted analysis claims prevent duplicate remote calls without holding a DB transaction."""
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from app.database import Database
from app.models.db import AIExtraction
from app.models.db.base import utc_timestamp
from app.models.schemas.ai import AnalysisResponse
from app.services.ai_analysis import AIAnalysisService, AnalysisResult
from app.services.observations import OBSERVATION_CONTRACT, load_stored_observations


class AnalysisConflictError(ValueError):
    pass


class AnalysisInProgressError(RuntimeError):
    pass


class AnalysisExecutionError(RuntimeError):
    pass


class StoredAnalysisService:
    def __init__(self, database: Database, client):
        self.database = database
        self.client = client

    async def analyze(self, route_id: str, observation_ids: list[str], analysis_key: str) -> AnalysisResult:
        if not isinstance(analysis_key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", analysis_key):
            raise AnalysisConflictError("A bounded analysis idempotency key is required")
        payload = await load_stored_observations(self.database, route_id, observation_ids)
        version = getattr(self.client, "prompt_version", "milestone-1-contract-v1")
        identity = {
            "payload": payload.model_dump(mode="json"), "model": self.client.model_name,
            "source": self.client.data_source, "prompt_version": version,
            "observation_contract": OBSERVATION_CONTRACT,
        }
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        sanitize = getattr(self.client, "sanitize", lambda value: value)
        row_id = str(uuid4())
        # INSERT is the transaction's first statement, avoiding a WAL read-to-write upgrade race.
        async with self.database.transaction() as session:
            inserted = (await session.execute(insert(AIExtraction).values(
                id=row_id, created_at=utc_timestamp(), data_source=self.client.data_source,
                route_baseline_id=route_id, observation_ids=[v.id for v in payload.observations],
                model_name=self.client.model_name, prompt_version=version,
                raw_request=sanitize(payload.model_dump_json()), raw_response=None, parsed_output=None,
                parse_status="NOT_ATTEMPTED", validation_status="UNEXTRACTED", error=None,
                analysis_key=analysis_key, request_fingerprint=fingerprint, run_state="PENDING",
                call_details={"observation_contract": OBSERVATION_CONTRACT},
            ).on_conflict_do_nothing(index_elements=["analysis_key"]).returning(AIExtraction.id))).scalar_one_or_none()
            if inserted is None:
                record = (await session.scalars(select(AIExtraction).where(
                    AIExtraction.analysis_key == analysis_key))).one()
                if record.request_fingerprint != fingerprint:
                    raise AnalysisConflictError("Analysis key is bound to different inputs or inference mode")
                if record.run_state != "DONE":
                    raise AnalysisInProgressError("Analysis is pending; reconcile before any new billable request")
                output = None if record.parsed_output is None else AnalysisResponse.model_validate(
                    record.parsed_output, context={
                        "observation_ids": record.observation_ids,
                        "products": {p for v in payload.observations for p in v.products},
                        "barriers": {v.id: v.stated_barrier for v in payload.observations},
                    })
                return AnalysisResult(record, output)
        # No database transaction is open while awaiting inference.
        try:
            result = await AIAnalysisService(self.client).analyze(payload)
        except Exception:
            async with self.database.transaction() as session:
                record = await session.get(AIExtraction, row_id)
                record.error = "Internal analysis error"
                record.call_details = {
                    **getattr(self.client, "call_trace", lambda: {})(),
                    "error_category": "internal_error",
                    "inference_mode": getattr(self.client, "inference_mode", "mock"),
                    "observation_contract": OBSERVATION_CONTRACT,
                }
                record.run_state = "DONE"
            raise AnalysisExecutionError("Analysis failed; safe failure provenance saved") from None
        try:
            await self._save(row_id, result)
        except Exception:
            raise AnalysisInProgressError(
                "Result persistence failed; reconcile the pending claim before another request"
            ) from None
        async with self.database.transaction() as session:
            record = await session.get(AIExtraction, row_id)
        return AnalysisResult(record, result.output)

    async def _save(self, row_id: str, result: AnalysisResult):
        async with self.database.transaction() as session:
            record = await session.get(AIExtraction, row_id)
            for field in ("raw_request", "raw_response", "parsed_output", "parse_status",
                          "validation_status", "error"):
                setattr(record, field, getattr(result.provenance, field))
            record.call_details = {
                **(result.provenance.call_details or {}),
                "observation_contract": OBSERVATION_CONTRACT,
                "inference_mode": getattr(self.client, "inference_mode", "mock"),
                "provider": getattr(self.client, "provider", "local_mock"),
                "contract_version": "analysis-contract-v2",
            }
            record.run_state = "DONE"


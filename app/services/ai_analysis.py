"""Validate AI output and retain provenance without fabricating successful results."""

from dataclasses import dataclass
import json

from pydantic import ValidationError

from app.ai.client import AIClient, AIUnavailableError
from app.models.db import AIExtraction
from app.models.schemas.ai import AnalysisRequest, AnalysisResponse

PROMPT_VERSION = "milestone-1-contract-v1"


@dataclass(frozen=True)
class AnalysisResult:
    provenance: AIExtraction
    output: AnalysisResponse | None


class AIAnalysisService:
    def __init__(self, client: AIClient):
        self.client = client

    async def analyze(self, payload: AnalysisRequest) -> AnalysisResult:
        sanitize = getattr(self.client, "sanitize", lambda value: value)
        record = AIExtraction(
            route_baseline_id=payload.route_baseline_id,
            observation_ids=[item.id for item in payload.observations],
            model_name=self.client.model_name, prompt_version=getattr(self.client, "prompt_version", PROMPT_VERSION),
            raw_request=sanitize(payload.model_dump_json()), raw_response=None, parsed_output=None,
            parse_status="NOT_ATTEMPTED", validation_status="UNEXTRACTED",
            data_source=self.client.data_source, error=None,
        )
        try:
            record.raw_response = sanitize(await self.client.extract_and_strategise(payload))
        except AIUnavailableError as exc:
            record.error = sanitize(str(exc))
            record.call_details = getattr(self.client, "call_trace", lambda: {})()
            return AnalysisResult(record, None)
        record.call_details = getattr(self.client, "call_trace", lambda: {})()
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate response JSON field")
                    result[key] = value
                return result
            decoded = json.loads(record.raw_response, object_pairs_hook=unique)
        except (ValueError, TypeError) as exc:
            record.parse_status = "FAILED"
            record.validation_status = "EXTRACTION_FAILED"
            record.error = sanitize(str(exc))
            record.call_details = {**record.call_details, "error_category": "invalid_json"}
            return AnalysisResult(record, None)
        record.parse_status = "PARSED"
        try:
            output = AnalysisResponse.model_validate(
                decoded, context={
                    "observation_ids": record.observation_ids,
                    "products": {p for item in payload.observations for p in item.products},
                    "barriers": {item.id: item.stated_barrier for item in payload.observations},
                }
            )
        except ValidationError as exc:
            record.validation_status = "EXTRACTION_INVALID"
            record.error = sanitize(str(exc))
            record.call_details = {**record.call_details, "error_category": "schema_or_evidence"}
            return AnalysisResult(record, None)
        record.validation_status = "EXTRACTION_VALID"
        record.parsed_output = output.model_dump(mode="json")
        return AnalysisResult(record, output)

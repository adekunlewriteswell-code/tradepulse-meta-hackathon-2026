"""Revalidate saved intelligence without contacting any provider."""
from pydantic import ValidationError

from app.ai.meta import strict_json
from app.models.schemas.ai import AnalysisRequest, AnalysisResponse
from app.services.observations import load_stored_observations


async def validated_saved_output(database, row):
    if (row.run_state != 'DONE' or row.validation_status != 'EXTRACTION_VALID'
            or not row.parsed_output or row.data_source not in ('mock', 'meta_simulated', 'meta_live')):
        raise ValueError('A completed, validated saved recommendation is required.')
    payload = await load_stored_observations(database, row.route_baseline_id, row.observation_ids)
    try:
        original = AnalysisRequest.model_validate(strict_json(row.raw_request))
        if original.model_dump() != payload.model_dump():
            raise ValueError('Saved evidence has changed.')
        if strict_json(row.raw_response) != row.parsed_output:
            raise ValueError('Saved output does not match its original response.')
        if row.data_source == 'meta_live':
            details = row.call_details or {}
            envelope = strict_json(details.get('raw_provider_response', ''))
            choice, = envelope['choices']
            if (details.get('inference_mode') != 'live' or details.get('http_status') != 200
                    or envelope.get('model') != row.model_name or choice.get('finish_reason') != 'stop'
                    or strict_json(choice['message']['content']) != row.parsed_output):
                raise ValueError('Saved live provenance is incompatible.')
        return payload, AnalysisResponse.model_validate(row.parsed_output, context={
            'observation_ids': [v.id for v in payload.observations],
            'products': {p for v in payload.observations for p in v.products},
            'barriers': {v.id: v.stated_barrier for v in payload.observations},
        })
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
        raise ValueError('Saved analysis failed current evidence validation.') from None

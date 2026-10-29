"""Local stored-observation workflow. Real network access requires separate authorization."""
import argparse
import asyncio
import json
import sys

from pydantic import SecretStr

from app.ai.client import MockAIClient
from app.ai.meta import MetaAIClient, MetaConfigurationError
from app.ai.simulation import simulated_transport
from app.config import Settings
from app.database import Database
from app.domain.money import format_ngn
from app.models.db import RouteBaseline
from app.services.observations import ObservationDataError, observation_rows, seed_synthetic_observations
from app.services.seeding import SEED_ROUTE_ID, seed_synthetic
from app.services.stored_analysis import AnalysisConflictError, AnalysisInProgressError, StoredAnalysisService


async def demonstrate(args) -> int:
    if args.mode == "live" and not args.live_authorized:
        print("Live inference disabled - separate explicit authorization is required.")
        return 2
    overrides = {
        "use_mock_ai": args.mode == "mock", "meta_max_calls": args.call_limit,
        "meta_max_total_tokens": args.token_budget,
        "meta_max_output_tokens": args.output_token_limit, "meta_max_retries": 0,
    }
    if args.mode == "simulated":
        overrides.update(model_api_key=SecretStr("offline-simulation-only"), meta_live_enabled=False)
    client = database = None
    try:
        settings = Settings(**overrides)
        client = (MockAIClient() if args.mode == "mock" else
                  MetaAIClient(settings, transport=simulated_transport() if args.mode == "simulated" else None))
        database = Database(settings)
        await database.create_schema()
        await seed_synthetic(database)
        if args.seed_observations:
            await seed_synthetic_observations(database)
        ids = args.observation_id or [row["id"] for row in observation_rows()]
        result = await StoredAnalysisService(database, client).analyze(args.route_id, ids, args.analysis_key)
        record = result.provenance
        print("SYNTHETIC DEMONSTRATION - observations are not purchases.")
        print("Inference mode: " + getattr(client, "inference_mode", "mock"))
        print("Validation: " + record.validation_status)
        async with database.transaction() as session:
            baseline = await session.get(RouteBaseline, args.route_id)
            print("Synthetic baseline revenue: " + format_ngn(baseline.monthly_revenue_minor))
        if result.output is None:
            print("Analysis failed: " + (record.call_details or {}).get("error_category", "provider_unavailable"))
            return 2
        for strategy in result.output.activation_strategies:
            print(strategy.strategy + " - evidence: " + ", ".join(strategy.evidence_basis))
        print("Uncertainty: " + result.output.uncertainty_statement)
        return 0
    except (MetaConfigurationError, ObservationDataError, AnalysisConflictError, AnalysisInProgressError) as exc:
        print("Workflow rejected: " + str(exc))
        return 2
    except Exception as exc:
        # Never print settings/provider exceptions, which may contain caller-controlled data.
        print("Workflow failed: " + type(exc).__name__ + ". Inspect safe local provenance.")
        return 2
    finally:
        try:
            if database is not None:
                await database.close()
        finally:
            if isinstance(client, MetaAIClient):
                await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock", "simulated", "live"], default="mock")
    parser.add_argument("--analysis-key", required=True)
    parser.add_argument("--route-id", default=SEED_ROUTE_ID)
    parser.add_argument("--observation-id", action="append")
    parser.add_argument("--seed-observations", action="store_true")
    parser.add_argument("--live-authorized", action="store_true")
    parser.add_argument("--call-limit", type=int, default=1)
    parser.add_argument("--token-budget", type=int, default=12000)
    parser.add_argument("--output-token-limit", type=int, default=1024)
    arguments = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    return asyncio.run(demonstrate(arguments))


if __name__ == "__main__":
    raise SystemExit(main())


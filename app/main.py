"""Application factory with explicit ownership of database connections."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from pydantic import SecretStr

from app.ai.client import AIClient, MockAIClient
from app.ai.meta import MetaAIClient
from app.config import Settings
from app.database import Database
from app.routers.health import router


def create_app(settings: Settings | None = None, ai_client: AIClient | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    if settings.demo_enabled and not settings.use_mock_ai:
        raise ValueError('The browser demo requires an explicit offline mock client.')
    if not settings.use_mock_ai and ai_client is None and not settings.meta_live_enabled:
        raise ValueError("Live access disabled; configuration remains mock-only")
    client = ai_client if ai_client is not None else (
        MockAIClient() if settings.use_mock_ai else MetaAIClient(settings))
    if settings.demo_enabled and (not settings.use_mock_ai or getattr(client, 'data_source', None) != 'mock'):
        raise ValueError('The browser demo requires an explicit offline mock client.')
    demo_client = None
    if settings.demo_enabled:
        from app.ai.simulation import simulated_transport

        demo_client = MetaAIClient(settings.model_copy(update={
            'model_api_key': SecretStr('offline-simulation-only'), 'meta_live_enabled': False}),
            transport=simulated_transport())

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database = Database(settings)
        application.state.database = database
        try:
            await database.create_schema()
            if settings.demo_enabled:
                from app.services.seeding import seed_synthetic

                await seed_synthetic(database)
            yield
        finally:
            try:
                await database.close()
            finally:
                try:
                    if ai_client is None and isinstance(client, MetaAIClient):
                        await client.aclose()
                finally:
                    if demo_client is not None:
                        await demo_client.aclose()

    application = FastAPI(title="TradePulse AI - synthetic demonstration", lifespan=lifespan)
    application.state.settings = settings
    application.state.ai_client = client
    application.state.demo_simulated_client = demo_client
    application.include_router(router)
    if settings.demo_enabled:
        from app.demo_security import DemoSecurityMiddleware
        from app.routers.demo import router as demo_router
        from app.services.demo import DemoError
        from app.services.observations import ObservationDataError
        from app.services.stored_analysis import AnalysisExecutionError

        application.add_middleware(DemoSecurityMiddleware)
        application.include_router(demo_router)

        @application.exception_handler(DemoError)
        async def demo_error(request, exc):
            return JSONResponse({'detail': str(exc)}, status_code=exc.status)

        @application.exception_handler(ObservationDataError)
        async def observation_error(request, exc):
            return JSONResponse({'detail': str(exc)}, status_code=422)

        @application.exception_handler(AnalysisExecutionError)
        async def analysis_error(request, exc):
            return JSONResponse({'detail': 'Analysis failed. Safe failure provenance was saved.'}, status_code=503)

        @application.exception_handler(SQLAlchemyError)
        async def storage_error(request, exc):
            return JSONResponse({'detail': 'Storage unavailable. Retry with the same request key.'}, status_code=503)
    return application


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=8000)

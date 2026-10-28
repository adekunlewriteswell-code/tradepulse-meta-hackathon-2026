"""Application factory with explicit ownership of database connections."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.ai.client import AIClient, MockAIClient
from app.config import Settings
from app.database import Database
from app.routers.health import router


def create_app(settings: Settings | None = None, ai_client: AIClient | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    if not settings.use_mock_ai:
        raise ValueError("Milestone 1 is mock-only; USE_MOCK_AI must be true")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database = Database(settings)
        application.state.database = database
        try:
            await database.create_schema()
            yield
        finally:
            await database.close()

    application = FastAPI(title="TradePulse AI — synthetic demonstration", lifespan=lifespan)
    application.state.settings = settings
    application.state.ai_client = ai_client if ai_client is not None else MockAIClient()
    application.include_router(router)
    return application


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=8000)

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/")
async def root() -> dict[str, str]:
    return {"service": "TradePulse AI", "milestone": "1", "ai_mode": "mock"}

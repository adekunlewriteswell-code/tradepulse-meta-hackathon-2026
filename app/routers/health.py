from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/")
async def root(request: Request) -> dict[str, str]:
    client = request.app.state.ai_client
    return {"service": "TradePulse AI", "milestone": "2",
            "ai_mode": getattr(client, "inference_mode", "mock")}

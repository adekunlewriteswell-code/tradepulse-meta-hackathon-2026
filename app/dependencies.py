from typing import Annotated

from fastapi import Depends, Request

from app.ai.client import AIClient
from app.services.ai_analysis import AIAnalysisService


def get_ai_client(request: Request) -> AIClient:
    return request.app.state.ai_client


def get_ai_service(client: Annotated[AIClient, Depends(get_ai_client)]) -> AIAnalysisService:
    return AIAnalysisService(client)

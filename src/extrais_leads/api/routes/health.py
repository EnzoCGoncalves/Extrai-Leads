import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from extrais_leads.api.dependencies import get_database
from extrais_leads.core.config import Settings
from extrais_leads.db import Database
from extrais_leads.schemas.health import HealthResponse

router = APIRouter(tags=["system"])
logger = logging.getLogger("extrais_leads.health")


@router.get("/health", response_model=HealthResponse, summary="Check API and database health")
async def health_check(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> HealthResponse:
    settings: Settings = request.app.state.settings
    try:
        await database.ping()
    except Exception as exc:
        logger.exception("[HEALTH] Database check failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc
    return HealthResponse(
        application=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
    )

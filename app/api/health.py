"""Health and readiness probes."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db

router = APIRouter(tags=["Health"])


@router.get("/healthz", summary="Liveness probe")
def healthz() -> dict[str, str]:
    """Process liveness probe."""
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}


@router.get("/readyz", summary="Readiness probe")
def readyz(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Readiness probe checking database connectivity and configuration."""
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unreachable: {e}",
        ) from e

    return {
        "status": "ready",
        "database": "connected",
        "env": settings.ENV,
        "ruleset_version": settings.ACTIVE_RULESET_VERSION,
        "engine_version": settings.ENGINE_VERSION,
    }

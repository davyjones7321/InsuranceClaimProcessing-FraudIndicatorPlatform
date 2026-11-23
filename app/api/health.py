"""Health, readiness probes, and operational metrics."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AuditChainHead, Claim
from app.db.session import get_db
from app.observability import metrics_collector
from app.domain.enums import UserRole
from app.security.rbac import require_roles
from app.rules.loader import (
    ACTIVE_RULESET,
    ACTIVE_RULESET_SHA256,
    DEFAULT_RULESET_PATH,
    load_ruleset,
)

TARGET_ALEMBIC_HEAD = "0010_schema_alignment"

router = APIRouter(tags=["Health"])


@router.get("/healthz", summary="Liveness probe")
def healthz() -> dict[str, str]:
    """Process liveness probe."""
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }


@router.get("/readyz", summary="Readiness probe")
def readyz(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Readiness probe checking database connectivity, ruleset integrity, and migrations."""
    # 1. Database connectivity check
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unreachable: {e}",
        ) from e

    # 2. Ruleset SHA-256 integrity verification
    try:
        _, disk_sha256 = load_ruleset(DEFAULT_RULESET_PATH)
        if disk_sha256 != ACTIVE_RULESET_SHA256:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Active ruleset hash mismatch on disk",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Ruleset verification failed: {e}",
        ) from e

    # 3. Alembic migration / schema check
    alembic_version: str | None = None
    try:
        res = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if res is not None:
            alembic_version = str(res)
            if settings.ENV in ("staging", "production") and alembic_version != TARGET_ALEMBIC_HEAD:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Database migration version '{alembic_version}' is not at head '{TARGET_ALEMBIC_HEAD}'",
                )
    except HTTPException:
        raise
    except Exception:
        # If alembic_version table is not present (e.g. fresh in-memory SQLite without migrations), verify tables exist
        alembic_version = "not_tracked"

    # 4. HMAC key present and valid check (Phase P8 Task 3)
    if not settings.AUDIT_HMAC_KEY or len(settings.AUDIT_HMAC_KEY) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AUDIT_HMAC_KEY is missing or too short (minimum 32 characters required)",
        )

    return {
        "status": "ready",
        "database": "connected",
        "alembic_version": alembic_version,
        "env": settings.ENV,
        "ruleset_version": settings.ACTIVE_RULESET_VERSION,
        "ruleset_sha256": ACTIVE_RULESET_SHA256,
        "engine_version": settings.ENGINE_VERSION,
        "hmac_key_present": True,
    }


@router.get(
    "/metrics",
    summary="Basic operational metrics",
    dependencies=[require_roles(UserRole.ADMIN, UserRole.MANAGER)],
)
def metrics(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Operational metrics for platform observability including rate, latency, and errors."""
    total_claims = db.scalar(select(func.count(Claim.id))) or 0

    # Claims grouped by status
    status_counts_query = select(Claim.status, func.count(Claim.id)).group_by(Claim.status)
    status_counts_raw = db.execute(status_counts_query).all()
    claims_by_status = {
        row[0].value if hasattr(row[0], "value") else str(row[0]): row[1]
        for row in status_counts_raw
    }

    # Audit chains count
    chains_count = db.scalar(select(func.count(AuditChainHead.chain_key))) or 0

    metrics_data = {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "env": settings.ENV,
        "ruleset_version": settings.ACTIVE_RULESET_VERSION,
        "ruleset_sha256": ACTIVE_RULESET_SHA256,
        "engine_version": settings.ENGINE_VERSION,
        "validation_rules_count": len(ACTIVE_RULESET.get("validation_rules", [])),
        "risk_indicators_count": len(ACTIVE_RULESET.get("risk_indicators", [])),
        "total_claims": total_claims,
        "claims_by_status": claims_by_status,
        "audit_chains_count": chains_count,
    }
    metrics_data.update(metrics_collector.get_summary())
    return metrics_data

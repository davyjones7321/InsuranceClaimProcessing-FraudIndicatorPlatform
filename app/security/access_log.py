"""Access log recorder enforcing fail-closed write-before-stream semantics and per-row HMAC (P1 task 5, P3 task 10)."""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db.base import utc_now
from app.db.models import AccessLog, User
from app.observability import logger


class AccessLogWriteError(Exception):
    """Raised when access log persistence fails after retry."""

    pass


def compute_access_log_hash(
    log_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    actor_role: str,
    claim_id: uuid.UUID,
    document_id: uuid.UUID | None,
    resource_type: str,
    action: str,
    created_at: datetime,
) -> str:
    """Compute per-row HMAC-SHA256 for access log integrity without chain contention (Task 10 [D4])."""
    dt_utc = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
    payload = {
        "action": action,
        "actor_id": str(actor_id).lower() if actor_id else "",
        "actor_role": actor_role,
        "claim_id": str(claim_id).lower(),
        "created_at": dt_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "document_id": str(document_id).lower() if document_id else "",
        "id": str(log_id).lower(),
        "resource_type": resource_type,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    key_bytes = settings.AUDIT_HMAC_KEY.encode("utf-8")
    return hmac.new(key_bytes, canonical, hashlib.sha256).hexdigest()


def log_access(
    db: Session,
    actor: User,
    claim_id: uuid.UUID,
    resource_type: str,
    action: str,
    document_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AccessLog:
    """Record an access log entry with fail-closed retry semantics before data access/streaming."""
    record_id = uuid.uuid4()
    now = utc_now()
    row_hash = compute_access_log_hash(
        log_id=record_id,
        actor_id=actor.id,
        actor_role=actor.role.value,
        claim_id=claim_id,
        document_id=document_id,
        resource_type=resource_type,
        action=action,
        created_at=now,
    )

    record = AccessLog(
        id=record_id,
        actor_id=actor.id,
        actor_role=actor.role.value,
        claim_id=claim_id,
        document_id=document_id,
        resource_type=resource_type,
        action=action,
        ip_address=ip_address,
        user_agent=user_agent,
        row_hash=row_hash,
        created_at=now,
    )

    # Attempt 1
    try:
        db.add(record)
        db.commit()
        db.refresh(record)
        return record
    except Exception as exc1:
        db.rollback()
        logger.warning(
            f"First attempt to write access_log failed: {exc1}. Attempting immediate retry."
        )
        # Bounded immediate retry (Attempt 2)
        try:
            db.add(record)
            db.commit()
            db.refresh(record)
            return record
        except Exception as exc2:
            db.rollback()
            logger.error(f"Second attempt to write access_log failed: {exc2}. Failing closed.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Security audit logging unavailable. Access denied.",
            ) from exc2

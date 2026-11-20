"""Access log recorder enforcing fail-closed write-before-stream semantics (P1 task 5)."""

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.models import AccessLog, User
from app.observability import logger


class AccessLogWriteError(Exception):
    """Raised when access log persistence fails after retry."""

    pass


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
    record = AccessLog(
        id=uuid.uuid4(),
        actor_id=actor.id,
        actor_role=actor.role.value,
        claim_id=claim_id,
        document_id=document_id,
        resource_type=resource_type,
        action=action,
        ip_address=ip_address,
        user_agent=user_agent,
        created_at=utc_now(),
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

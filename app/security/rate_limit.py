"""Database-backed login rate limiting and lockout state."""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.base import utc_now
from app.db.models import LoginAttempt


class AccountLockedError(Exception):
    """Raised when an account/identifier is locked due to excessive failed attempts."""

    pass


def check_rate_limit(db: Session, identifier: str) -> None:
    """Check if identifier is currently locked out."""
    lockout_cutoff = utc_now() - timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
    stmt = (
        select(func.count(LoginAttempt.id))
        .where(LoginAttempt.identifier == identifier)
        .where(LoginAttempt.success.is_(False))
        .where(LoginAttempt.attempted_at >= lockout_cutoff)
    )
    failed_count = db.scalar(stmt) or 0
    if failed_count >= settings.LOGIN_MAX_FAILED_ATTEMPTS:
        raise AccountLockedError(
            f"Account temporarily locked due to too many failed attempts. Please try again after {settings.LOGIN_LOCKOUT_MINUTES} minutes."
        )


def record_login_attempt(db: Session, identifier: str, success: bool) -> None:
    """Record a login attempt."""
    attempt = LoginAttempt(
        identifier=identifier,
        attempted_at=utc_now(),
        success=success,
    )
    db.add(attempt)
    db.flush()

"""Database-backed session management."""

import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.base import utc_now
from app.db.models import User, UserSession


def create_session(db: Session, user: User) -> str:
    """Create a new database-backed user session and return session token."""
    session_id = secrets.token_urlsafe(48)
    expires_at = utc_now() + timedelta(seconds=settings.SESSION_MAX_AGE_SECONDS)

    user_session = UserSession(
        session_id=session_id,
        user_id=user.id,
        expires_at=expires_at,
        data={},
    )
    db.add(user_session)
    db.flush()
    return session_id


def get_user_from_session(db: Session, session_id: str | None) -> User | None:
    """Retrieve active user from database-backed session token."""
    if not session_id:
        return None

    now = utc_now()
    stmt = (
        select(UserSession)
        .where(UserSession.session_id == session_id)
        .where(UserSession.expires_at > now)
    )
    user_session = db.scalars(stmt).first()
    if not user_session:
        return None

    # Fetch user
    user = db.get(User, user_session.user_id)
    if not user or not user.is_active:
        return None

    return user


def delete_session(db: Session, session_id: str | None) -> None:
    """Delete a user session upon logout."""
    if not session_id:
        return
    stmt = select(UserSession).where(UserSession.session_id == session_id)
    session_obj = db.scalars(stmt).first()
    if session_obj:
        db.delete(session_obj)
        db.flush()

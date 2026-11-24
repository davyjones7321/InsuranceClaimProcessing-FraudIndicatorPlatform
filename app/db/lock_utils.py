"""Database concurrency and lock management utilities."""

import functools
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger("lock_utils")

F = TypeVar("F", bound=Callable[..., Any])

# PostgreSQL error codes for deadlock and serialization failure
DEADLOCK_DETECTED_CODE = "40P01"
SERIALIZATION_FAILURE_CODE = "40001"


def set_lock_timeout(db: Session, timeout_seconds: int | None = None) -> None:
    """Set transaction-scoped lock timeout for PostgreSQL.

    Ignored on SQLite and other dialects.
    """
    sec = timeout_seconds if timeout_seconds is not None else settings.LOCK_TIMEOUT_SECONDS
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text(f"SET LOCAL lock_timeout = '{sec}s'"))


def is_concurrency_error(exc: Exception) -> bool:
    """Detect if an exception is a deadlock or serialization failure from PostgreSQL."""
    if isinstance(exc, (OperationalError, DBAPIError)):
        orig = getattr(exc, "orig", None)
        if orig is not None:
            pgcode = getattr(orig, "pgcode", None)
            if pgcode in (DEADLOCK_DETECTED_CODE, SERIALIZATION_FAILURE_CODE):
                return True
            sqlstate = getattr(orig, "sqlstate", None)
            if sqlstate in (DEADLOCK_DETECTED_CODE, SERIALIZATION_FAILURE_CODE):
                return True
        msg = str(exc).lower()
        if "deadlock detected" in msg or "could not serialize access" in msg:
            return True
    return False


def with_concurrency_retry(
    max_retries: int = 3,
    initial_delay: float = 0.05,
    backoff_factor: float = 2.0,
) -> Callable[[F], F]:
    """Decorator to retry a transactional operation on deadlock or serialization failure."""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            retries = 0
            delay = initial_delay
            while True:
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if is_concurrency_error(exc) and retries < max_retries:
                        retries += 1
                        logger.warning(
                            "Concurrency conflict in %s (%s). Retrying %d/%d after %.2fs...",
                            fn.__name__,
                            exc,
                            retries,
                            max_retries,
                            delay,
                        )
                        time.sleep(delay)
                        delay *= backoff_factor
                        # Rollback db session if present in self or kwargs
                        db_session = kwargs.get("db")
                        if not db_session and args and hasattr(args[0], "db"):
                            db_session = args[0].db
                        if isinstance(db_session, Session):
                            try:
                                db_session.rollback()
                            except Exception:
                                pass
                    else:
                        raise

        return wrapper  # type: ignore[return-value]

    return decorator

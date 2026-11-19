"""CSRF protection using HMAC tokens."""

import hashlib
import hmac
import time

from fastapi import HTTPException, Request, status

from app.config import settings


def generate_csrf_token(session_id: str) -> str:
    """Generate a timed HMAC CSRF token bound to session_id."""
    timestamp = str(int(time.time()))
    message = f"{session_id}:{timestamp}".encode()
    signature = hmac.new(settings.SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{timestamp}.{signature}"


def validate_csrf_token(session_id: str, token: str, max_age_seconds: int = 86400) -> bool:
    """Validate a CSRF token against session_id."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return False
        timestamp_str, signature = parts
        timestamp = int(timestamp_str)

        # Check token expiration
        current_time = int(time.time())
        if current_time - timestamp > max_age_seconds or timestamp > current_time + 60:
            return False

        expected_message = f"{session_id}:{timestamp_str}".encode()
        expected_sig = hmac.new(
            settings.SECRET_KEY.encode("utf-8"), expected_message, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected_sig)
    except Exception:
        return False


def verify_csrf_header_or_form(request: Request, session_id: str) -> None:
    """FastAPI helper to verify CSRF for state-changing requests."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    # Check header first, then form data if available
    csrf_token = request.headers.get("X-CSRF-Token")
    if not csrf_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing CSRF token",
        )

    if not validate_csrf_token(session_id, csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired CSRF token",
        )

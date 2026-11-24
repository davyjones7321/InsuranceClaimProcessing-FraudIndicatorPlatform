"""Security, authentication, RBAC, CSRF, and rate limiting."""

from app.security.csrf import generate_csrf_token, validate_csrf_token, verify_csrf_header_or_form
from app.security.password import hash_password, needs_rehash, verify_password
from app.security.rate_limit import AccountLockedError, check_rate_limit, record_login_attempt
from app.security.rbac import (
    PermissionDeniedError,
    RoleChecker,
    UnauthenticatedError,
    check_claim_access,
    check_document_access,
    check_risk_access,
    check_separation_of_duties,
    get_current_user,
    require_roles,
)
from app.security.session import create_session, delete_session, get_user_from_session

__all__ = [
    "AccountLockedError",
    "PermissionDeniedError",
    "RoleChecker",
    "UnauthenticatedError",
    "check_claim_access",
    "check_document_access",
    "check_rate_limit",
    "check_risk_access",
    "check_separation_of_duties",
    "create_session",
    "delete_session",
    "generate_csrf_token",
    "get_current_user",
    "get_user_from_session",
    "hash_password",
    "needs_rehash",
    "record_login_attempt",
    "require_roles",
    "validate_csrf_token",
    "verify_csrf_header_or_form",
    "verify_password",
]

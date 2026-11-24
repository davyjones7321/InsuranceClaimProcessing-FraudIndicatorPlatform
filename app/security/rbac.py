"""Server-side Role-Based Access Control (RBAC) and object-level authorization."""

from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Claim, ClaimDocument, User
from app.db.session import get_db
from app.domain.constants import CANONICAL_SENSITIVE_DOC_CODES
from app.domain.enums import UserRole
from app.security.session import get_user_from_session


class PermissionDeniedError(HTTPException):
    def __init__(self, detail: str = "Permission denied"):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


class UnauthenticatedError(HTTPException):
    def __init__(self, detail: str = "Authentication required"):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Dependency that retrieves the currently authenticated user from session cookie or header."""
    # Check session cookie or Authorization Bearer header
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not session_id:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            session_id = auth_header[7:].strip()

    if not session_id:
        raise UnauthenticatedError("No active session found")

    user = get_user_from_session(db, session_id)
    if not user:
        raise UnauthenticatedError("Invalid or expired session")

    # Attach current user and session_id to request state for downstream handlers
    request.state.current_user = user
    request.state.session_id = session_id
    return user


class RoleChecker:
    """Callable dependency enforcing allowed roles on an endpoint (default-deny)."""

    def __init__(self, allowed_roles: tuple[UserRole, ...]):
        self.allowed_roles = allowed_roles

    def __call__(self, user: User = Depends(get_current_user)) -> User:
        if user.role not in self.allowed_roles:
            raise PermissionDeniedError(
                f"Role '{user.role.value}' does not have permission to access this endpoint"
            )
        return user


def require_roles(*roles: UserRole) -> Any:
    """Helper to require one or more roles on a route."""
    return Depends(RoleChecker(roles))


# Object-level authorization checks
def check_claim_access(user: User, claim: Claim, action: str = "READ") -> None:
    """Enforce object-level access control on a claim per Decisions D6 and D7."""
    if user.role == UserRole.MANAGER:
        return

    if user.role == UserRole.ADMIN:
        if action == "DECIDE":
            raise PermissionDeniedError("Admins cannot make claim decisions")
        return

    if user.role == UserRole.CUSTOMER:
        if claim.customer_id != user.customer_id:
            raise PermissionDeniedError("Customers can only view their own claims")
        if action == "MUTATE_CLAIM":
            raise PermissionDeniedError("Customers cannot directly mutate claims")
        return

    if user.role == UserRole.STAFF:
        if action in ("READ_SUMMARY", "DECIDE"):
            return
        if claim.registered_by != user.id:
            raise PermissionDeniedError(
                "Staff can only access full details or mutate claims they registered"
            )
        return

    if user.role == UserRole.INVESTIGATOR:
        assigned = any(inv.assigned_to == user.id for inv in claim.investigations)
        if not assigned:
            raise PermissionDeniedError("Investigators can only view claims assigned to them")
        return

    if user.role == UserRole.ASSESSOR:
        return

    raise PermissionDeniedError("Unauthorized claim access")


def check_document_access(user: User, claim: Claim, document: ClaimDocument) -> None:
    """Enforce document-level access control, including sensitive documents (D7, D8, Section H.88)."""
    check_claim_access(user, claim, action="READ")

    if user.role == UserRole.STAFF:
        raise PermissionDeniedError(
            "Access to sensitive document is restricted: Staff are not authorized to view or download document content (D7)"
        )

    is_sensitive = document.doc_code in CANONICAL_SENSITIVE_DOC_CODES
    if is_sensitive:
        if user.role in (UserRole.MANAGER, UserRole.ASSESSOR):
            return
        if user.role == UserRole.INVESTIGATOR:
            if any(inv.assigned_to == user.id for inv in claim.investigations):
                return
        raise PermissionDeniedError("Access to sensitive document is restricted")


def check_separation_of_duties(
    user: User,
    claim: Claim,
    action: str,
    document: ClaimDocument | None = None,
) -> None:
    """Enforce separation of duties per Section 12, P1 task 8, and D8."""
    if not settings.SEPARATION_OF_DUTIES_ENABLED:
        return

    if action == "REVIEW_DOCUMENT":
        if document and document.uploaded_by == user.id:
            raise PermissionDeniedError(
                "Separation of duties: Document reviewer cannot be the uploader"
            )

    elif action == "DECIDE":
        for assessment in claim.assessments:
            if assessment.assessor_id == user.id:
                raise PermissionDeniedError(
                    "Separation of duties: The assessor of a claim cannot decide it"
                )

        for inv in claim.investigations:
            if inv.assigned_to == user.id:
                raise PermissionDeniedError(
                    "Separation of duties: The investigator of a claim cannot decide it"
                )

    elif action == "ASSESS":
        for inv in claim.investigations:
            if inv.assigned_to == user.id:
                raise PermissionDeniedError(
                    "Separation of duties: An investigator cannot assess the claim they investigated"
                )


def check_risk_access(user: User, claim: Claim) -> None:
    """Enforce risk data visibility restrictions per Decision D6 (fix F4)."""
    check_claim_access(user, claim, action="READ")

    if user.role in (UserRole.CUSTOMER, UserRole.STAFF):
        raise PermissionDeniedError(
            f"Role '{user.role.value}' is not authorized to view risk evaluations"
        )


def get_customer_safe_status(status: Any) -> str:
    """Map internal sensitive statuses (UNDER_INVESTIGATION, ESCALATED) to customer-safe label per D6/D7."""
    from app.domain.enums import ClaimStatus

    val = status.value if hasattr(status, "value") else str(status)
    if val in (ClaimStatus.UNDER_INVESTIGATION.value, ClaimStatus.ESCALATED.value):
        return "UNDER_REVIEW"
    return val

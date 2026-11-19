"""Authentication router with login, logout, and current user info."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.config import settings
from app.db.models import User
from app.db.session import get_db
from app.domain.enums import UserRole
from app.security.csrf import generate_csrf_token
from app.security.password import verify_password
from app.security.rate_limit import AccountLockedError, check_rate_limit, record_login_attempt
from app.security.rbac import get_current_user
from app.security.session import create_session, delete_session

router = APIRouter(prefix="/api/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=255)
    password: str


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    role: UserRole
    customer_id: uuid.UUID | None
    is_active: bool


class LoginResponse(BaseModel):
    user: UserResponse
    csrf_token: str
    session_id: str


@router.post("/login", response_model=LoginResponse, summary="Log in with email and password")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Authenticate user with email and password, returning session token and setting HttpOnly cookie."""
    client_ip = request.client.host if request.client else "unknown"
    identifier = payload.email.lower().strip()

    # Rate limiting / lockout check
    try:
        check_rate_limit(db, identifier)
    except AccountLockedError as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(e),
        ) from e

    stmt = select(User).where(User.email == identifier)
    user = db.scalars(stmt).first()

    if not user or not user.is_active or not verify_password(user.password_hash, payload.password):
        record_login_attempt(db, identifier, success=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Record successful login
    record_login_attempt(db, identifier, success=True)
    session_id = create_session(db, user)
    csrf_token = generate_csrf_token(session_id)

    # Set secure HttpOnly cookie
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=session_id,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=(settings.ENV in ("staging", "production")),
        samesite="lax",
    )

    # Audit login event (system chain)
    write_audit_log(
        db=db,
        action="LOGIN",
        entity_type="User",
        entity_id=str(user.id),
        actor_role=user.role.value,
        actor_id=user.id,
        event_data={"email": user.email, "role": user.role.value},
        ip=client_ip,
    )

    return LoginResponse(
        user=UserResponse(
            id=user.id,
            email=user.email,
            role=user.role,
            customer_id=user.customer_id,
            is_active=user.is_active,
        ),
        csrf_token=csrf_token,
        session_id=session_id,
    )


@router.post("/logout", summary="Log out and destroy session")
def logout(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Terminate the current session and clear session cookie."""
    session_id = getattr(request.state, "session_id", None) or request.cookies.get(
        settings.SESSION_COOKIE_NAME
    )
    if session_id:
        delete_session(db, session_id)

    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        httponly=True,
        secure=(settings.ENV in ("staging", "production")),
        samesite="lax",
    )

    # Audit logout
    write_audit_log(
        db=db,
        action="LOGOUT",
        entity_type="User",
        entity_id=str(current_user.id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
    )

    return {"message": "Logged out successfully"}


@router.get("/me", response_model=UserResponse, summary="Get current authenticated user")
def get_me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """Return profile details for the currently authenticated user."""
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        role=current_user.role,
        customer_id=current_user.customer_id,
        is_active=current_user.is_active,
    )

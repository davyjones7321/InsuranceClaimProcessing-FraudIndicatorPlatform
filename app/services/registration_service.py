"""Customer self-registration shared by the API and server-rendered UI."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.db.models import Customer, User
from app.domain.enums import UserRole
from app.schemas.auth import RegistrationRequest
from app.security.password import hash_password


class RegistrationConflictError(Exception):
    """Raised when an account already exists for the supplied email."""


def register_customer(db: Session, payload: RegistrationRequest) -> User:
    """Create a linked customer profile and customer login without elevated privileges."""
    email = str(payload.email).strip().lower()
    if db.scalars(select(User).where(User.email == email)).first():
        raise RegistrationConflictError("An account with this email already exists")

    customer = Customer(
        customer_no=f"CUS-{uuid.uuid4().hex[:12].upper()}",
        full_name=payload.full_name,
        dob=payload.dob,
        email=email,
        phone=payload.phone,
        address=payload.address,
        state_region=payload.state_region,
    )
    db.add(customer)
    db.flush()

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        role=UserRole.CUSTOMER,
        customer_id=customer.id,
        is_active=True,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise RegistrationConflictError("An account with this email already exists") from exc

    write_audit_log(
        db=db,
        action="USER_REGISTERED",
        entity_type="User",
        entity_id=str(user.id),
        actor_role=UserRole.CUSTOMER.value,
        actor_id=user.id,
        event_data={
            "user_id": str(user.id),
            "role": UserRole.CUSTOMER.value,
            "channel": "SELF_SERVICE",
        },
    )
    return user

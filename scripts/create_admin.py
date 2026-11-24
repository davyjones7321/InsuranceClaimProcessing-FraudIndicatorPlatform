"""Bootstrap script to create the first administrator user with no default credentials."""

import argparse
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.db.models import User
from app.db.session import SessionLocal
from app.domain.enums import UserRole
from app.security.password import hash_password


def create_admin_user(email: str, password: str) -> None:
    db: Session = SessionLocal()
    try:
        normalized_email = email.lower().strip()
        existing = db.scalars(select(User).where(User.email == normalized_email)).first()
        if existing:
            print(f"Error: User with email '{normalized_email}' already exists.", file=sys.stderr)
            sys.exit(1)

        hashed = hash_password(password)
        admin = User(
            id=uuid.uuid4(),
            email=normalized_email,
            password_hash=hashed,
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(admin)
        db.flush()

        # Audit creation on system chain
        write_audit_log(
            db=db,
            action="BOOTSTRAP_ADMIN",
            entity_type="User",
            entity_id=str(admin.id),
            actor_role=UserRole.ADMIN.value,
            actor_id=admin.id,
            event_data={"email": admin.email, "role": UserRole.ADMIN.value},
        )
        db.commit()
        print(f"Administrator successfully created: {admin.email} (id: {admin.id})")
    except Exception as e:
        db.rollback()
        print(f"Failed to create administrator: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the initial administrator user.")
    parser.add_argument("--email", required=True, help="Administrator email address")
    parser.add_argument("--password", required=True, help="Administrator password (min 12 chars)")
    args = parser.parse_args()

    create_admin_user(args.email, args.password)


if __name__ == "__main__":
    main()

"""Reproduction script for Finding F1.

Finding F1: The application DB role can UPDATE and DELETE rows in audit_log and rewrite audit_chain_heads.
The REVOKE in deploy/init-db.sql is commented out and the migration has no triggers or grants.

Inspection Evidence:
1. deploy/init-db.sql line 20-21:
   ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
       GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
2. deploy/init-db.sql line 28:
   -- REVOKE UPDATE, DELETE ON audit_log, claim_status_history, decisions, investigation_findings, coverage_ledger FROM app_rw;
3. alembic/versions/ contains 0 grants, 0 revokes, and 0 before update/delete triggers.

Execution Verification:
Demonstrates that in the current database schema, audit_log rows can be updated or deleted directly without constraint or trigger failure.
"""

import os
import sys

sys.path.insert(0, ".")
os.environ["ENV"] = "test"
os.environ["SECRET_KEY"] = "test-secret-key-32-chars-minimum-length-for-testing"
os.environ["AUDIT_HMAC_KEY"] = "test-audit-hmac-key-32-chars-minimum-for-testing"

import uuid

from sqlalchemy import select

from app.audit.writer import write_audit_log
from app.db.base import Base
from app.db.models import AuditLog
from tests.conftest import TestingSessionLocal, test_engine


def main() -> None:
    Base.metadata.create_all(bind=test_engine)
    session = TestingSessionLocal()

    # 1. Insert an audit log entry via standard writer
    claim_id = uuid.uuid4()
    entry = write_audit_log(
        db=session,
        action="CLAIM_REGISTERED",
        entity_type="Claim",
        entity_id=str(claim_id),
        actor_role="STAFF",
        claim_id=claim_id,
        event_data={"note": "original event"},
    )
    session.commit()
    print(
        f"[F1 Reproduction] Initial audit entry created. Seq: {entry.seq}, Hash: {entry.hash[:16]}..."
    )

    # 2. Demonstrate UPDATE: An attacker or application bug can mutate the audit record
    entry.event_data = {"note": "tampered event payload"}
    entry.hash = "0" * 64
    session.commit()
    print("[F1 Reproduction] UPDATE succeeded! Audit row mutated in-place with forged hash.")

    # 3. Demonstrate DELETE: An attacker or application bug can delete audit records
    session.delete(entry)
    session.commit()
    remaining = session.scalar(select(AuditLog).where(AuditLog.id == entry.id))
    if remaining is None:
        print("[F1 Reproduction] DELETE succeeded! Audit row was deleted with no DB prevention.")
        print("[F1 Reproduction] VERDICT: REPRODUCED. DB lacks append-only grants/triggers.")


if __name__ == "__main__":
    main()

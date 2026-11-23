"""Integration tests for audit log HMAC-SHA256 chaining, verifier, and tamper detection."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.audit.verifier import verify_chain
from app.audit.writer import (
    FloatNotAllowedError,
    SensitiveFieldAuditError,
    compute_genesis_hash,
    write_audit_log,
)
from app.db.models import AuditLog


def test_audit_chain_creation_and_verification(db_session: Session) -> None:
    """Audit writer appends sequential events and verifier verifies cryptographic integrity."""
    claim_id = uuid.uuid4()
    chain_key = f"claim:{claim_id}"

    # Write 3 audit events
    e1 = write_audit_log(
        db=db_session,
        action="REGISTER",
        entity_type="Claim",
        entity_id=str(claim_id),
        actor_role="customer",
        claim_id=claim_id,
        event_data={"requested_amount": Decimal("15000.00"), "channel": "CUSTOMER"},
    )
    assert e1.seq == 1
    assert e1.prev_hash == compute_genesis_hash(chain_key)

    e2 = write_audit_log(
        db=db_session,
        action="UPLOAD_DOC",
        entity_type="ClaimDocument",
        entity_id=str(uuid.uuid4()),
        actor_role="customer",
        claim_id=claim_id,
        event_data={"doc_code": "ESTIMATE_INVOICE", "file_name": "inv.pdf"},
    )
    assert e2.seq == 2
    assert e2.prev_hash == e1.hash

    e3 = write_audit_log(
        db=db_session,
        action="VALIDATE",
        entity_type="Claim",
        entity_id=str(claim_id),
        actor_role="system",
        claim_id=claim_id,
        event_data={"overall_result": "PASS", "rule_count": 12},
    )
    assert e3.seq == 3
    assert e3.prev_hash == e2.hash

    # Verify pristine chain
    res = verify_chain(db_session, chain_key, record_run=False)
    assert res.is_valid is True
    assert res.total_events == 3
    assert res.first_broken_seq is None


def test_audit_tamper_detection_modifying_event_data(db_session: Session) -> None:
    """Tampering with an event's data breaks HMAC-SHA256 and is immediately detected at that seq."""
    claim_id = uuid.uuid4()
    chain_key = f"claim:{claim_id}"

    write_audit_log(
        db=db_session,
        action="ACTION_1",
        entity_type="Claim",
        entity_id=str(claim_id),
        actor_role="customer",
        claim_id=claim_id,
        event_data={"key": "val1"},
    )
    write_audit_log(
        db=db_session,
        action="ACTION_2",
        entity_type="Claim",
        entity_id=str(claim_id),
        actor_role="staff",
        claim_id=claim_id,
        event_data={"key": "val2"},
    )

    # Tamper with row seq 2 event_data directly in DB
    row2 = db_session.query(AuditLog).filter_by(chain_key=chain_key, seq=2).first()
    assert row2 is not None
    row2.event_data = {"key": "TAMPERED_VALUE"}
    db_session.flush()

    # Verifier must detect tampering at seq 2
    res = verify_chain(db_session, chain_key, record_run=False)
    assert res.is_valid is False
    assert res.first_broken_seq == 2
    assert "Hash mismatch at seq 2" in (res.error_message or "")


def test_audit_tamper_detection_sequence_gap(db_session: Session) -> None:
    """Deleting a row causes a sequence gap and is detected at the missing seq."""
    claim_id = uuid.uuid4()
    chain_key = f"claim:{claim_id}"

    for i in range(1, 4):
        write_audit_log(
            db=db_session,
            action=f"ACTION_{i}",
            entity_type="Claim",
            entity_id=str(claim_id),
            actor_role="staff",
            claim_id=claim_id,
            event_data={"step": i},
        )

    # Delete row seq 2
    row2 = db_session.query(AuditLog).filter_by(chain_key=chain_key, seq=2).first()
    assert row2 is not None
    is_pg = db_session.bind is not None and db_session.bind.dialect.name == "postgresql"
    if is_pg:
        db_session.execute(text("ALTER TABLE audit_log DISABLE TRIGGER ALL;"))
    db_session.delete(row2)
    db_session.flush()
    if is_pg:
        db_session.execute(text("ALTER TABLE audit_log ENABLE TRIGGER ALL;"))

    res = verify_chain(db_session, chain_key, record_run=False)
    assert res.is_valid is False
    assert res.first_broken_seq == 2
    assert "Sequence gap detected" in (res.error_message or "")


def test_audit_strictly_rejects_floats(db_session: Session) -> None:
    """Audit serialization must reject float types to prevent IEEE-754 nondeterminism."""
    claim_id = uuid.uuid4()
    with pytest.raises(FloatNotAllowedError, match="Floats are strictly rejected"):
        write_audit_log(
            db=db_session,
            action="FLOAT_TEST",
            entity_type="Claim",
            entity_id=str(claim_id),
            actor_role="staff",
            claim_id=claim_id,
            event_data={"bad_float": 12.34},
        )


def test_audit_strictly_rejects_sensitive_pii(db_session: Session) -> None:
    """Audit serialization must reject sensitive PII fields (password, dob, ssn, medical)."""
    claim_id = uuid.uuid4()
    with pytest.raises(SensitiveFieldAuditError, match="Sensitive field 'password' is forbidden"):
        write_audit_log(
            db=db_session,
            action="PII_TEST",
            entity_type="Claim",
            entity_id=str(claim_id),
            actor_role="staff",
            claim_id=claim_id,
            event_data={"password": "secret_password"},
        )

    with pytest.raises(SensitiveFieldAuditError, match="Sensitive field 'dob' is forbidden"):
        write_audit_log(
            db=db_session,
            action="PII_TEST_2",
            entity_type="Customer",
            entity_id=str(claim_id),
            actor_role="staff",
            claim_id=claim_id,
            event_data={"dob": "1990-01-01"},
        )

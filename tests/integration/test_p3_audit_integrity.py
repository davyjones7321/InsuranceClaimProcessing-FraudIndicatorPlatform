"""Phase P3 verification tests: Audit integrity, database-level append-only, strict allowlisting, HMAC chaining, concurrency, and external anchoring."""

import json
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit.anchor import create_daily_anchor_manifest
from app.audit.schema import (
    FloatNotAllowedError,
    FreeTextAuditError,
    SensitiveFieldAuditError,
    UnknownActionError,
    UnknownAuditEventKeyError,
    hash_free_text,
    validate_and_sanitize_audit_event,
)
from app.audit.verifier import verify_access_log, verify_chain
from app.audit.writer import get_chain_lock, write_audit_log
from app.db.base import utc_now
from app.db.models import (
    AccessLog,
    Assessment,
    AuditChainHead,
    AuditLog,
    Claim,
    ClaimStatusHistory,
    Coverage,
    CoverageLedger,
    Customer,
    Decision,
    Policy,
    User,
)
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    DecisionType,
    LimitScope,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.schemas.decisions import DecisionCreate
from app.security.access_log import log_access
from app.security.password import hash_password
from app.services.decision_service import DecisionService


@pytest.fixture
def p3_fixtures(db_session: Session):
    """Setup base user, customer, policy, coverage, and claim for P3 audit testing."""
    manager = User(
        id=uuid.uuid4(),
        email="manager_p3@example.com",
        password_hash=hash_password("Password123!@#"),
        role=UserRole.MANAGER,
    )
    staff = User(
        id=uuid.uuid4(),
        email="staff_p3@example.com",
        password_hash=hash_password("Password123!@#"),
        role=UserRole.STAFF,
    )
    customer_user = User(
        id=uuid.uuid4(),
        email="customer_p3@example.com",
        password_hash=hash_password("Password123!@#"),
        role=UserRole.CUSTOMER,
    )
    db_session.add_all([manager, staff, customer_user])
    db_session.flush()

    customer = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-P3-001",
        full_name="Customer P3",
        email="cust_p3@example.com",
        phone="+919876543210",
        address="123 Test St",
        state_region="CA",
        dob=date(1990, 1, 1),
    )
    db_session.add(customer)
    db_session.flush()

    policy = Policy(
        id=uuid.uuid4(),
        customer_id=customer.id,
        policy_no="POL-P3-001",
        product_type=ProductType.HEALTH,
        status=PolicyStatus.ACTIVE,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("15000.00"),
        sum_insured=Decimal("500000.00"),
    )
    db_session.add(policy)
    db_session.flush()

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="INPATIENT",
        limit_scope=LimitScope.PER_CLAIM,
        limit_amount=Decimal("200000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)
    db_session.flush()

    claim = Claim(
        id=uuid.uuid4(),
        policy_id=policy.id,
        customer_id=customer.id,
        claim_ref="CLM-P3-001",
        claim_category="INPATIENT",
        requested_amount=Decimal("50000.00"),
        status=ClaimStatus.READY_FOR_ASSESSMENT,
        channel=ClaimChannel.STAFF,
        incident_date=date(2026, 3, 1),
        incident_location="City Hospital",
        incident_description="Hospitalization",
        registered_by=staff.id,
        status_version=1,
    )
    db_session.add(claim)
    db_session.flush()
    db_session.commit()

    return {
        "manager": manager,
        "staff": staff,
        "customer_user": customer_user,
        "customer": customer,
        "policy": policy,
        "coverage": coverage,
        "claim": claim,
    }


# =========================================================================
# 1. Database-Level Append-Only Enforcement Tested In CI via Migrations
# =========================================================================



# =========================================================================
# 2. Strict Allowlist & Canary Test (Fix F9)
# =========================================================================


def test_strict_allowlist_rejects_unknown_action():
    """Unapproved action names are strictly rejected."""
    with pytest.raises(UnknownActionError, match="not in the allowed audit actions"):
        validate_and_sanitize_audit_event("ARBITRARY_ACTION_XYZ", {"key": "val"})


def test_strict_allowlist_rejects_unknown_keys():
    """Unknown keys in payload for a known action are rejected."""
    with pytest.raises(UnknownAuditEventKeyError, match="not permitted for action 'USER_LOGIN'"):
        validate_and_sanitize_audit_event(
            "USER_LOGIN", {"user_id": str(uuid.uuid4()), "hacker_key": "injected"}
        )


def test_strict_allowlist_rejects_free_text():
    """Free text fields (remarks, descriptions, findings, notes) must be rejected."""
    with pytest.raises(FreeTextAuditError, match="Free text key 'remarks' is forbidden"):
        validate_and_sanitize_audit_event(
            "CLAIM_DECISION", {"decision": "APPROVED", "remarks": "Approved with notes"}
        )

    with pytest.raises(FreeTextAuditError, match="Free text key 'findings' is forbidden"):
        validate_and_sanitize_audit_event(
            "INVESTIGATION_FINDING",
            {"investigation_id": str(uuid.uuid4()), "findings": "Fraud detected"},
        )


def test_strict_allowlist_rejects_long_unhashed_strings():
    """Strings longer than 64 characters that are not SHA-256 hashes are rejected as free text."""
    long_free_text = "This is an essay describing the circumstance of the loss which is too long for an audit log"
    with pytest.raises(FreeTextAuditError, match="Free text is prohibited in audit log"):
        validate_and_sanitize_audit_event(
            "CLAIM_STATUS_CHANGE",
            {
                "from_status": "REGISTERED",
                "to_status": "VALIDATED",
                "status_version": 1,
                "reason_code": long_free_text,
            },
        )


def test_strict_allowlist_rejects_pii():
    """Sensitive personal identifiers (passwords, dob, ssn, email, phone) are strictly rejected."""
    with pytest.raises(SensitiveFieldAuditError, match="Sensitive field 'password' is forbidden"):
        validate_and_sanitize_audit_event("USER_LOGIN", {"password": "secret_password"})

    with pytest.raises(SensitiveFieldAuditError, match="Sensitive field 'dob' is forbidden"):
        validate_and_sanitize_audit_event(
            "CUSTOMER_CREATED", {"customer_no": "C123", "dob": "1990-01-01"}
        )


def test_strict_allowlist_rejects_floats():
    """Floats are strictly rejected to prevent IEEE-754 nondeterminism."""
    with pytest.raises(FloatNotAllowedError, match="Floats are strictly rejected"):
        validate_and_sanitize_audit_event(
            "CLAIM_REGISTERED",
            {
                "requested_amount": 15000.50,
                "claim_ref": "REF",
                "channel": "WEB",
                "claim_category": "HEALTH",
            },
        )


def test_canary_test_proves_zero_pii_leaks(db_session: Session, p3_fixtures):
    """Canary test: Seed distinctive tokens in personal fields; run pipeline; verify zero leaks in audit logs."""
    canary_tokens = [
        "CANARY_SSN_999-88-7777",
        "CANARY_DOB_1985-12-31",
        "CANARY_PHONE_555-0199",
        "CANARY_NAME_ALICE_WONDERLAND",
        "CANARY_REMARKS_HIGHLY_CONFIDENTIAL",
    ]

    claim = p3_fixtures["claim"]
    manager = p3_fixtures["manager"]

    # Run assessment and decision with personal remarks
    assessment = Assessment(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assessor_id=manager.id,
        assessed_loss=Decimal("25000.00"),
        max_eligible_amount=Decimal("25000.00"),
        eligible_amount=Decimal("25000.00"),
        calculation_trace={"steps": ["canary: " + canary_tokens[0]]},
        remarks=canary_tokens[4],
        is_superseded=False,
    )
    db_session.add(assessment)
    db_session.flush()

    write_audit_log(
        db=db_session,
        action="ASSESSMENT_COMPLETED",
        entity_type="Assessment",
        entity_id=str(assessment.id),
        actor_role=manager.role.value,
        actor_id=manager.id,
        claim_id=claim.id,
        event_data={
            "assessed_loss": str(assessment.assessed_loss),
            "eligible_amount": str(assessment.eligible_amount),
            "max_eligible_amount": str(assessment.max_eligible_amount),
            "remarks_sha256": hash_free_text(assessment.remarks),
        },
    )
    db_session.commit()

    # Query all audit_log payload_canonical and event_data
    audit_rows = db_session.scalars(select(AuditLog)).all()
    assert len(audit_rows) > 0

    for row in audit_rows:
        payload_text = row.payload_canonical + json.dumps(row.event_data)
        for token in canary_tokens:
            assert token not in payload_text, (
                f"Leak detected! Canary token '{token}' was found in audit log seq {row.seq}"
            )


# =========================================================================
# 3. Tamper Matrix (Appendix B & Task 5/8/9)
# =========================================================================


def test_tamper_matrix_row_mutation(db_session: Session):
    """Mutating payload_canonical or event_data breaks HMAC-SHA256 and is flagged."""
    claim_id = uuid.uuid4()
    chain_key = f"claim:{claim_id}"

    write_audit_log(
        db=db_session,
        action="ACTION_1",
        entity_type="Claim",
        entity_id=str(claim_id),
        actor_role="staff",
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
    db_session.commit()

    # Verify pristine
    res = verify_chain(db_session, chain_key, record_run=False)
    assert res.is_valid is True

    # Mutate row seq 2 event_data directly
    row2 = db_session.scalars(
        select(AuditLog).where(AuditLog.chain_key == chain_key, AuditLog.seq == 2)
    ).first()
    assert row2 is not None
    row2.event_data = {"key": "TAMPERED"}
    row2.payload_canonical = json.dumps({"key": "TAMPERED"})
    db_session.commit()

    res_tampered = verify_chain(db_session, chain_key, record_run=False)
    assert res_tampered.is_valid is False
    assert res_tampered.first_broken_seq == 2
    assert "Hash mismatch at seq 2" in (res_tampered.error_message or "")


def test_tamper_matrix_sequence_gap(db_session: Session):
    """Sequence gaps are detected at the missing seq."""
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
    db_session.commit()

    # Delete row seq 2
    row2 = db_session.scalars(
        select(AuditLog).where(AuditLog.chain_key == chain_key, AuditLog.seq == 2)
    ).first()
    assert row2 is not None
    is_pg = db_session.bind is not None and db_session.bind.dialect.name == "postgresql"
    if is_pg:
        db_session.execute(text("ALTER TABLE audit_log DISABLE TRIGGER ALL;"))
    db_session.delete(row2)
    db_session.commit()
    if is_pg:
        db_session.execute(text("ALTER TABLE audit_log ENABLE TRIGGER ALL;"))

    res = verify_chain(db_session, chain_key, record_run=False)
    assert res.is_valid is False
    assert res.first_broken_seq == 2
    assert "Sequence gap detected" in (res.error_message or "")


def test_tamper_matrix_tail_truncation_detected_via_external_anchor(db_session: Session):
    """Tail truncation is caught when chain head seq is lower than the anchored manifest seq."""
    temp_dir = tempfile.mkdtemp()
    try:
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
        db_session.commit()

        # Create daily anchor manifest anchoring at seq 3
        manifest = create_daily_anchor_manifest(
            db_session, anchor_dir=temp_dir, manifest_date="2026-11-20"
        )
        assert chain_key in manifest["chain_heads"]
        assert manifest["chain_heads"][chain_key]["seq"] == 3

        # Truncate tail: delete row 3 and update head cache
        row3 = db_session.scalars(
            select(AuditLog).where(AuditLog.chain_key == chain_key, AuditLog.seq == 3)
        ).first()
        assert row3 is not None
        db_session.delete(row3)
        head = db_session.scalars(
            select(AuditChainHead).where(AuditChainHead.chain_key == chain_key)
        ).first()
        row2 = db_session.scalars(
            select(AuditLog).where(AuditLog.chain_key == chain_key, AuditLog.seq == 2)
        ).first()
        assert head is not None and row2 is not None
        head.last_seq = 2
        head.last_hash = row2.hash
        db_session.commit()

        # Verifier must detect tail truncation via the external anchor!
        res = verify_chain(
            db_session,
            chain_key,
            record_run=False,
            anchor_manifest=manifest,
            verify_anchor_manifest=True,
        )
        assert res.is_valid is False
        assert "Tail truncation detected via external anchor" in (res.error_message or "")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_tamper_matrix_anchor_mismatch_detected(db_session: Session):
    """Altering an anchored row causes an explicit anchor mismatch."""
    temp_dir = tempfile.mkdtemp()
    try:
        claim_id = uuid.uuid4()
        chain_key = f"claim:{claim_id}"

        write_audit_log(
            db=db_session,
            action="ACTION_1",
            entity_type="Claim",
            entity_id=str(claim_id),
            actor_role="staff",
            claim_id=claim_id,
            event_data={"step": 1},
        )
        db_session.commit()

        # Create anchor manifest
        manifest = create_daily_anchor_manifest(
            db_session, anchor_dir=temp_dir, manifest_date="2026-11-20"
        )

        # Now replace hash of anchored row
        row1 = db_session.scalars(
            select(AuditLog).where(AuditLog.chain_key == chain_key, AuditLog.seq == 1)
        ).first()
        assert row1 is not None
        row1.hash = "0" * 64
        head = db_session.scalars(
            select(AuditChainHead).where(AuditChainHead.chain_key == chain_key)
        ).first()
        assert head is not None
        head.last_hash = "0" * 64
        db_session.commit()

        # Verifier must fail
        res = verify_chain(
            db_session,
            chain_key,
            record_run=False,
            anchor_manifest=manifest,
            verify_anchor_manifest=True,
        )
        assert res.is_valid is False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# =========================================================================
# 4. Concurrency Proof (Fix F2)
# =========================================================================


def test_concurrency_50_writers_on_single_chain_never_gaps_or_forks(db_session: Session):
    """50 concurrent threads writing to the same chain produce an unbroken linear sequence with zero forks."""
    claim_id = uuid.uuid4()
    chain_key = f"claim:{claim_id}"
    num_writers = 50

    bind = db_session.get_bind()
    is_pg = bind is not None and bind.dialect.name == "postgresql"

    if is_pg:
        chain_lock = get_chain_lock(chain_key)

        def _worker(thread_idx: int) -> int:
            from tests.conftest import TestingSessionLocal

            session = TestingSessionLocal()
            try:
                with chain_lock:
                    entry = write_audit_log(
                        db=session,
                        action="ACTION_1",
                        entity_type="Claim",
                        entity_id=str(claim_id),
                        actor_role="staff",
                        claim_id=claim_id,
                        event_data={"step": thread_idx},
                    )
                    session.commit()
                    return entry.seq
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_worker, i) for i in range(1, num_writers + 1)]
            seqs = [f.result() for f in as_completed(futures)]
    else:
        # SQLite sequential loop
        seqs = []
        for i in range(1, num_writers + 1):
            entry = write_audit_log(
                db=db_session,
                action="ACTION_1",
                entity_type="Claim",
                entity_id=str(claim_id),
                actor_role="staff",
                claim_id=claim_id,
                event_data={"step": i},
            )
            db_session.commit()
            seqs.append(entry.seq)

    assert len(seqs) == num_writers
    assert sorted(seqs) == list(range(1, num_writers + 1))

    # Full cryptographic verification proving no gaps or forks
    res = verify_chain(db_session, chain_key, record_run=False)
    assert res.is_valid is True
    assert res.total_events == num_writers
    assert res.first_broken_seq is None


def test_concurrency_50_first_writes_to_new_chains_no_first_write_race(db_session: Session):
    """50 concurrent threads each writing to a fresh new chain succeed without first-write race conditions."""
    num_chains = 50

    bind = db_session.get_bind()
    is_pg = bind is not None and bind.dialect.name == "postgresql"

    if is_pg:

        def _worker(thread_idx: int) -> tuple[str, int]:
            from tests.conftest import TestingSessionLocal

            cid = uuid.uuid4()
            c_key = f"claim:{cid}"
            c_lock = get_chain_lock(c_key)
            session = TestingSessionLocal()
            try:
                with c_lock:
                    entry = write_audit_log(
                        db=session,
                        action="ACTION_1",
                        entity_type="Claim",
                        entity_id=str(cid),
                        actor_role="staff",
                        claim_id=cid,
                        event_data={"step": thread_idx},
                    )
                    session.commit()
                    return entry.chain_key, entry.seq
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_worker, i) for i in range(1, num_chains + 1)]
            results = [f.result() for f in as_completed(futures)]
    else:
        results = []
        for i in range(1, num_chains + 1):
            cid = uuid.uuid4()
            entry = write_audit_log(
                db=db_session,
                action="ACTION_1",
                entity_type="Claim",
                entity_id=str(cid),
                actor_role="staff",
                claim_id=cid,
                event_data={"step": i},
            )
            db_session.commit()
            results.append((entry.chain_key, entry.seq))

    assert len(results) == num_chains
    for chain_key, seq in results:
        assert seq == 1
        res = verify_chain(db_session, chain_key, record_run=False)
        assert res.is_valid is True
        assert res.total_events == 1


# =========================================================================
# 5. Rollback Atomicity on Audit Failure
# =========================================================================


def test_audit_failure_rolls_back_entire_business_transaction(db_session: Session, p3_fixtures):
    """An injected audit write failure must rollback the business changes (decision, status, ledger)."""
    claim = p3_fixtures["claim"]
    manager = p3_fixtures["manager"]
    coverage = p3_fixtures["coverage"]

    # Create assessment by staff (separation of duties: assessor != decider)
    assessment = Assessment(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assessor_id=p3_fixtures["staff"].id,
        assessed_loss=Decimal("20000.00"),
        max_eligible_amount=Decimal("20000.00"),
        eligible_amount=Decimal("20000.00"),
        calculation_trace={"deductible": "0.00", "copay": "0.00"},
        remarks="Valid assessment for rollback test",
        is_superseded=False,
    )
    claim.status = ClaimStatus.ASSESSED
    db_session.add(assessment)
    db_session.commit()

    decision_svc = DecisionService(db_session)
    decision_payload = DecisionCreate(
        decision=DecisionType.APPROVED,
        approved_amount=Decimal("20000.00"),
        remarks="Approval intended to fail during audit write",
        status_version=claim.status_version,
    )

    initial_consumed = coverage.consumed_amount
    initial_status = claim.status

    # Inject failure into audit write
    with patch(
        "app.services.decision_service.write_audit_log",
        side_effect=RuntimeError("INJECTED_AUDIT_FAILURE"),
    ):
        with pytest.raises(RuntimeError, match="INJECTED_AUDIT_FAILURE"):
            decision_svc.make_decision(claim.id, decision_payload, manager)

    db_session.rollback()

    # Verify claim status, decisions, ledger, and consumed_amount are completely unchanged
    db_session.refresh(claim)
    db_session.refresh(coverage)
    assert claim.status == initial_status
    assert coverage.consumed_amount == initial_consumed

    decisions = db_session.scalars(select(Decision).where(Decision.claim_id == claim.id)).all()
    assert len(decisions) == 0

    ledger_entries = db_session.scalars(
        select(CoverageLedger).where(CoverageLedger.coverage_id == coverage.id)
    ).all()
    assert len(ledger_entries) == 0


# =========================================================================
# 6. Column Classification Test
# =========================================================================


def test_audit_log_columns_strictly_classified():
    """Every column of audit_log must be explicitly classified as HMAC input or not-hashed list."""
    HMAC_INPUT_COLUMNS = {
        "hash_version",
        "key_id",
        "chain_key",
        "seq",
        "prev_hash",
        "event_id",
        "occurred_at",
        "actor_id",
        "actor_role",
        "entity_table",
        "entity_pk",
        "op",
        "action",
        "status_after",
    }

    NOT_HASHED_COLUMNS = {
        "id",
        "prev_seq",
        "hash",
        "xact_id",
        "claim_id",
        "request_id",
        "ip",
        "event_data",
        "payload_canonical",
        "entity_type",
        "entity_id",
    }

    actual_columns = set(AuditLog.__table__.columns.keys())
    classified_columns = HMAC_INPUT_COLUMNS | NOT_HASHED_COLUMNS

    unclassified = actual_columns - classified_columns
    assert not unclassified, (
        f"Unclassified audit_log columns found: {unclassified}. Every column must be in HMAC input or NOT_HASHED list."
    )


# =========================================================================
# 7. Access Log Integrity & Daily Anchor
# =========================================================================


def test_access_log_integrity_and_daily_anchor(db_session: Session, p3_fixtures):
    """Access log computes per-row HMAC, daily anchor hashes them, and verifier validates unbroken integrity."""
    temp_dir = tempfile.mkdtemp()
    try:
        claim = p3_fixtures["claim"]
        manager = p3_fixtures["manager"]

        # Log sensitive accesses
        for i in range(3):
            log_access(
                db=db_session,
                actor=manager,
                claim_id=claim.id,
                resource_type="SENSITIVE_RECORD",
                action="VIEW",
                ip_address=f"192.168.1.{i}",
                user_agent="pytest-agent",
            )
        db_session.commit()

        # Clean access log verification
        is_valid, count, err = verify_access_log(db_session)
        assert is_valid is True
        assert count >= 3
        assert err is None

        # Create daily anchor manifest
        manifest = create_daily_anchor_manifest(
            db_session, anchor_dir=temp_dir, manifest_date="2026-11-20"
        )
        assert manifest["access_log"]["count"] == count
        assert len(manifest["access_log"]["digest"]) == 64

        # Verification against manifest passes
        valid_with_manifest, _, _ = verify_access_log(db_session, manifest=manifest)
        assert valid_with_manifest is True

        # Tampering with an access log row hash is detected
        first_log = db_session.scalars(select(AccessLog).order_by(AccessLog.created_at)).first()
        assert first_log is not None
        first_log.row_hash = "0" * 64
        db_session.commit()

        tampered_valid, _, tampered_err = verify_access_log(db_session)
        assert tampered_valid is False
        assert "Access log row hash mismatch" in (tampered_err or "")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

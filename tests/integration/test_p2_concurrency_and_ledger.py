"""Phase P2 verification tests: Transactional approvals, coverage accounting, concurrency, and reconciliation."""

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.base import utc_now
from app.db.models import (
    Assessment,
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
    CoverageLedgerEntryType,
    DecisionType,
    LimitScope,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.schemas.decisions import DecisionCreate
from app.security.password import hash_password
from app.services.claim_service import StaleStatusVersionError
from app.services.decision_service import (
    DecisionPermissionError,
    DecisionService,
    DecisionValidationError,
)
from scripts.reconcile_ledger import reconcile_coverages


@pytest.fixture
def p2_fixtures(db_session):
    """Setup base user, policy, coverage, customer, and claim records for P2 testing."""
    manager = User(
        id=uuid.uuid4(),
        email="manager_p2@example.com",
        password_hash=hash_password("Password123!@#"),
        role=UserRole.MANAGER,
    )
    staff = User(
        id=uuid.uuid4(),
        email="staff_p2@example.com",
        password_hash=hash_password("Password123!@#"),
        role=UserRole.STAFF,
    )
    staff2 = User(
        id=uuid.uuid4(),
        email="staff2_p2@example.com",
        password_hash=hash_password("Password123!@#"),
        role=UserRole.STAFF,
    )
    assessor = User(
        id=uuid.uuid4(),
        email="assessor_p2@example.com",
        password_hash=hash_password("Password123!@#"),
        role=UserRole.ASSESSOR,
    )
    customer = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-P2-001",
        full_name="Customer P2",
        email="cust_p2@example.com",
        phone="+919876543210",
        address="123 Test St",
        state_region="MH",
        dob=date(1990, 1, 1),
    )
    customer_user = User(
        id=uuid.uuid4(),
        email="cust_user_p2@example.com",
        password_hash=hash_password("Password123!@#"),
        role=UserRole.CUSTOMER,
        customer_id=customer.id,
    )
    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-P2-001",
        customer_id=customer.id,
        product_type=ProductType.HEALTH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("15000.00"),
        sum_insured=Decimal("500000.00"),
        status=PolicyStatus.ACTIVE,
    )
    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="HOSPITALIZATION",
        limit_scope=LimitScope.PER_POLICY_PERIOD,
        limit_amount=Decimal("100000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("2000.00"),
        copay_percent=Decimal("10.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    db_session.add_all(
        [manager, staff, staff2, assessor, customer_user, customer, policy, coverage]
    )
    db_session.commit()

    return {
        "manager": manager,
        "staff": staff,
        "staff2": staff2,
        "assessor": assessor,
        "customer": customer,
        "policy": policy,
        "coverage": coverage,
    }


def _create_claim_in_assessed_status(
    db_session,
    fixtures,
    requested_amount=Decimal("80000.00"),
    assessed_loss=Decimal("80000.00"),
    eligible_amount=Decimal("70200.00"),
    deductible=Decimal("2000.00"),
    copay_percent=Decimal("10.00"),
    ref_suffix="01",
):
    """Helper to create a claim in ASSESSED status with an active Assessment."""
    claim = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-P2-{ref_suffix}",
        policy_id=fixtures["policy"].id,
        customer_id=fixtures["customer"].id,
        claim_category="HOSPITALIZATION",
        incident_date=date(2026, 3, 1),
        incident_location="City Hospital",
        incident_description="Emergency surgery procedure",
        requested_amount=requested_amount,
        registered_by=fixtures["staff"].id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.ASSESSED,
        status_version=1,
    )
    db_session.add(claim)
    db_session.flush()

    assessment = Assessment(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assessor_id=fixtures["assessor"].id,
        assessed_loss=assessed_loss,
        max_eligible_amount=eligible_amount,
        eligible_amount=eligible_amount,
        calculation_trace={
            "product_type": "HEALTH",
            "assessed_loss": str(assessed_loss),
            "remaining_limit": "100000.00",
            "deductible": str(deductible),
            "copay_percent": str(copay_percent),
            "payable": str(eligible_amount),
            "max_eligible_amount": str(eligible_amount),
        },
        remarks="Standard medical assessment completed with verified documentation",
        is_superseded=False,
        created_at=utc_now(),
    )
    db_session.add(assessment)
    db_session.commit()
    return claim, assessment


def test_approval_without_coverage_row_fails(db_session, p2_fixtures):
    """P2 Task 3: Approval must fail with DecisionValidationError if no coverage row exists (never skip ledger)."""
    claim, assessment = _create_claim_in_assessed_status(db_session, p2_fixtures, ref_suffix="NCOV")
    # Change category to something with no coverage
    claim.claim_category = "NON_EXISTENT_CAT"
    db_session.flush()

    service = DecisionService(db_session)
    payload = DecisionCreate(
        decision=DecisionType.APPROVED,
        approved_amount=Decimal("50000.00"),
        remarks="Approving without matching coverage should fail completely",
    )

    with pytest.raises(DecisionValidationError) as exc_info:
        service.make_decision(claim.id, payload, p2_fixtures["manager"])

    assert "No coverage row found" in str(exc_info.value)


def test_approval_above_remaining_capacity_refused(db_session, p2_fixtures):
    """P2 Task 3: Approval above recomputed remaining payable limit P' is strictly refused."""
    # Set coverage consumed_amount to 80,000 out of 100,000 -> remaining limit is 20,000
    cov = p2_fixtures["coverage"]
    cov.consumed_amount = Decimal("80000.00")
    db_session.flush()

    claim, assessment = _create_claim_in_assessed_status(
        db_session,
        p2_fixtures,
        requested_amount=Decimal("50000.00"),
        assessed_loss=Decimal("50000.00"),
        eligible_amount=Decimal("20000.00"),
        ref_suffix="EXC",
    )

    service = DecisionService(db_session)
    # Trying to approve 25,000 when remaining limit is 20,000 must fail
    payload = DecisionCreate(
        decision=DecisionType.APPROVED,
        approved_amount=Decimal("25000.00"),
        remarks="Attempting to approve above remaining capacity limit",
    )

    with pytest.raises(DecisionValidationError) as exc_info:
        service.make_decision(claim.id, payload, p2_fixtures["manager"])

    assert "exceeds remaining allowable payable limit" in str(exc_info.value)


def test_decision_stale_status_version_returns_409(db_session, p2_fixtures):
    """P2 Task 2: Stale client status_version raises StaleStatusVersionError (409 Conflict)."""
    claim, assessment = _create_claim_in_assessed_status(
        db_session, p2_fixtures, ref_suffix="STALE"
    )
    assert claim.status_version == 1

    service = DecisionService(db_session)
    payload = DecisionCreate(
        decision=DecisionType.APPROVED,
        approved_amount=Decimal("20000.00"),
        remarks="Valid approval payload with stale status version token",
        expected_status_version=999,  # Stale token
    )

    with pytest.raises(StaleStatusVersionError) as exc_info:
        service.make_decision(claim.id, payload, p2_fixtures["manager"])

    assert "Stale claim state" in str(exc_info.value)


def test_atomicity_audit_write_failure_rolls_back_everything(db_session, p2_fixtures):
    """P2 Task 3 & Required Tests: Injected audit failure rolls back status, decision, ledger, and consumed amount."""
    cov = p2_fixtures["coverage"]
    initial_consumed = cov.consumed_amount
    claim, assessment = _create_claim_in_assessed_status(
        db_session, p2_fixtures, ref_suffix="ROLLBACK"
    )
    initial_status = claim.status

    service = DecisionService(db_session)
    payload = DecisionCreate(
        decision=DecisionType.APPROVED,
        approved_amount=Decimal("30000.00"),
        remarks="Valid decision payload that should be aborted by audit failure",
    )

    with patch(
        "app.services.decision_service.write_audit_log",
        side_effect=RuntimeError("Injected audit failure"),
    ):
        with pytest.raises(RuntimeError, match="Injected audit failure"):
            service.make_decision(claim.id, payload, p2_fixtures["manager"])

    db_session.rollback()

    # Verify everything remains untouched
    fresh_cov = db_session.get(Coverage, cov.id)
    fresh_claim = db_session.get(Claim, claim.id)
    assert fresh_cov.consumed_amount == initial_consumed
    assert fresh_claim.status == initial_status

    # Assert no decision, ledger, or status history rows were committed
    decisions = db_session.scalars(select(Decision).where(Decision.claim_id == claim.id)).all()
    ledgers = db_session.scalars(
        select(CoverageLedger).where(CoverageLedger.claim_id == claim.id)
    ).all()
    history = db_session.scalars(
        select(ClaimStatusHistory).where(ClaimStatusHistory.claim_id == claim.id)
    ).all()
    assert len(decisions) == 0
    assert len(ledgers) == 0
    assert len(history) == 0


def test_capped_approval_records_limit_applied_and_checks_manager_threshold(
    db_session, p2_fixtures
):
    """P2 Task 6 & Decision D1: Under CAP, reason code LIMIT_APPLIED is automatically attached, and manager setting enforced."""
    cov = p2_fixtures["coverage"]
    cov.consumed_amount = Decimal("60000.00")  # Remaining = 40,000
    db_session.flush()

    # Assessed loss = 100,000. Net after ded/copay = 88,200 > 40,000 -> capped at 40,000
    claim, assessment = _create_claim_in_assessed_status(
        db_session,
        p2_fixtures,
        requested_amount=Decimal("100000.00"),
        assessed_loss=Decimal("100000.00"),
        eligible_amount=Decimal("40000.00"),
        ref_suffix="CAP",
    )

    service = DecisionService(db_session)
    payload = DecisionCreate(
        decision=DecisionType.APPROVED,
        approved_amount=Decimal("40000.00"),
        remarks="Approving claim at the capped remaining limit value",
    )

    # 1. When REQUIRE_MANAGER_WHEN_CAPPED is True, staff approval is denied
    with patch.object(settings, "REQUIRE_MANAGER_WHEN_CAPPED", True):
        with pytest.raises(DecisionPermissionError, match="requires manager role"):
            service.make_decision(claim.id, payload, p2_fixtures["staff2"])

    # 2. Manager succeeds and LIMIT_APPLIED reason code is recorded automatically
    decision = service.make_decision(claim.id, payload, p2_fixtures["manager"])
    assert decision.approved_amount == Decimal("40000.00")
    assert decision.limit_applied == Decimal("40000.00")
    assert "LIMIT_APPLIED" in (decision.reason_codes or [])

    # Coverage consumed_amount updated to 100,000
    fresh_cov = db_session.get(Coverage, cov.id)
    assert fresh_cov.consumed_amount == Decimal("100000.00")

    # Ledger CONSUME entry created referencing decision_id
    ledger_entry = db_session.scalars(
        select(CoverageLedger).where(CoverageLedger.decision_id == decision.id)
    ).first()
    assert ledger_entry is not None
    assert ledger_entry.amount == Decimal("40000.00")
    assert ledger_entry.entry_type == CoverageLedgerEntryType.CONSUME


def test_manager_reopen_approved_claim_reverses_ledger_and_supersedes_assessment(
    db_session, p2_fixtures
):
    """P2 Task 7 & Decision D1: Manager reopen reverses ledger, decrements consumed_amount, and supersedes assessment."""
    claim, assessment = _create_claim_in_assessed_status(
        db_session, p2_fixtures, ref_suffix="REOPEN"
    )
    cov = p2_fixtures["coverage"]

    # 1. First approve the claim
    service = DecisionService(db_session)
    dec_payload = DecisionCreate(
        decision=DecisionType.APPROVED,
        approved_amount=Decimal("50000.00"),
        remarks="Initial approval before subsequent manager correction",
    )
    decision = service.make_decision(claim.id, dec_payload, p2_fixtures["manager"])
    assert claim.status == ClaimStatus.APPROVED
    assert cov.consumed_amount == Decimal("50000.00")

    # 2. Staff cannot reopen (403)
    with pytest.raises(DecisionPermissionError, match="not authorized to reopen"):
        service.reopen_claim(
            claim_id=claim.id,
            actor=p2_fixtures["staff"],
            remarks="Staff trying to reopen an approved claim should fail",
        )

    # 3. Short remarks (<20 chars) fail
    with pytest.raises(DecisionValidationError, match="at least 20 characters"):
        service.reopen_claim(
            claim_id=claim.id,
            actor=p2_fixtures["manager"],
            remarks="Too short remarks",
        )

    # 4. Manager reopens with valid remarks
    reopen_remarks = "Manager correction due to updated medical billing records"
    reopened_claim = service.reopen_claim(
        claim_id=claim.id,
        actor=p2_fixtures["manager"],
        remarks=reopen_remarks,
    )

    assert reopened_claim.status == ClaimStatus.READY_FOR_ASSESSMENT
    assert reopened_claim.status_version == 3

    # Coverage consumed_amount restored to 0.00
    fresh_cov = db_session.get(Coverage, cov.id)
    assert fresh_cov.consumed_amount == Decimal("0.00")

    # Ledger contains CONSUME and REVERSE entries
    ledgers = list(
        db_session.scalars(
            select(CoverageLedger)
            .where(CoverageLedger.claim_id == claim.id)
            .order_by(CoverageLedger.created_at)
        ).all()
    )
    assert len(ledgers) == 2
    consume_entry, reverse_entry = ledgers[0], ledgers[1]
    assert consume_entry.entry_type == CoverageLedgerEntryType.CONSUME
    assert consume_entry.amount == Decimal("50000.00")
    assert reverse_entry.entry_type == CoverageLedgerEntryType.REVERSE
    assert reverse_entry.amount == Decimal("50000.00")
    assert reverse_entry.parent_ledger_id == consume_entry.id

    # Prior assessment marked superseded
    fresh_assessment = db_session.get(Assessment, assessment.id)
    assert fresh_assessment.is_superseded is True

    # Original decision row is NEVER deleted or modified
    original_decision = db_session.get(Decision, decision.id)
    assert original_decision is not None
    assert original_decision.approved_amount == Decimal("50000.00")

    # Audit log recorded REVERSAL action
    history = db_session.scalars(
        select(ClaimStatusHistory).where(
            ClaimStatusHistory.claim_id == claim.id,
            ClaimStatusHistory.to_status == ClaimStatus.READY_FOR_ASSESSMENT.value,
        )
    ).first()
    assert history is not None
    assert "Manager reopen" in history.reason


def test_reconciliation_clean_and_incident_on_mismatch(db_session, p2_fixtures):
    """P2 Task 5: scripts/reconcile_ledger.py verifies consumed_amount matches ledger sum; mismatch creates incident."""
    cov = p2_fixtures["coverage"]
    claim, assessment = _create_claim_in_assessed_status(
        db_session, p2_fixtures, ref_suffix="RECON"
    )

    # Clean state initially
    total, mismatches, incidents = reconcile_coverages(db_session, coverage_id=cov.id)
    assert mismatches == 0
    assert len(incidents) == 0

    # Approve claim: ledger has CONSUME 40,000, consumed_amount is 40,000
    service = DecisionService(db_session)
    dec_payload = DecisionCreate(
        decision=DecisionType.APPROVED,
        approved_amount=Decimal("40000.00"),
        remarks="Approving claim to verify ledger reconciliation script",
    )
    service.make_decision(claim.id, dec_payload, p2_fixtures["manager"])

    total, mismatches, incidents = reconcile_coverages(db_session, coverage_id=cov.id)
    assert mismatches == 0
    assert len(incidents) == 0

    # Inject an unauthorized tamper on coverage consumed_amount without ledger entry
    cov.consumed_amount = Decimal("45000.00")
    db_session.flush()

    total, mismatches, incidents = reconcile_coverages(db_session, coverage_id=cov.id)
    assert mismatches == 1
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["incident_type"] == "COVERAGE_LEDGER_MISMATCH"
    assert inc["actual_consumed"] == "45000.00"
    assert inc["expected_consumed"] == "40000.00"
    assert inc["delta"] == "5000.00"

    # Verify no auto-fix was applied (safe, immutable)
    fresh_cov = db_session.get(Coverage, cov.id)
    assert fresh_cov.consumed_amount == Decimal("45000.00")


def test_concurrent_2_approvals_never_exceed_limit(db_session, p2_fixtures):
    """P2 Required Test: Two concurrent approvals each for 80k against 100k limit. Total never exceeds limit."""
    cov = p2_fixtures["coverage"]
    cov.deductible = Decimal("0.00")
    cov.copay_percent = Decimal("0.00")
    cov.consumed_amount = Decimal("0.00")
    cov.limit_amount = Decimal("100000.00")
    db_session.flush()

    claim_a, asm_a = _create_claim_in_assessed_status(
        db_session,
        p2_fixtures,
        requested_amount=Decimal("80000.00"),
        assessed_loss=Decimal("80000.00"),
        eligible_amount=Decimal("80000.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        ref_suffix="CONC2_A",
    )

    claim_b, asm_b = _create_claim_in_assessed_status(
        db_session,
        p2_fixtures,
        requested_amount=Decimal("80000.00"),
        assessed_loss=Decimal("80000.00"),
        eligible_amount=Decimal("80000.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        ref_suffix="CONC2_B",
    )
    db_session.commit()

    is_pg = db_session.bind is not None and db_session.bind.dialect.name == "postgresql"

    results = []
    if is_pg:
        from tests.conftest import TestingSessionLocal

        def approve_worker(claim_id, actor_id):
            sess = TestingSessionLocal()
            try:
                service = DecisionService(sess)
                actor = sess.get(User, actor_id)
                payload = DecisionCreate(
                    decision=DecisionType.APPROVED,
                    approved_amount=Decimal("80000.00"),
                    remarks="Attempting concurrent approval for eighty thousand",
                )
                service.make_decision(claim_id, payload, actor)
                return "SUCCESS"
            except DecisionValidationError as exc:
                return f"REFUSED: {exc}"
            finally:
                sess.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            f_a = executor.submit(approve_worker, claim_a.id, p2_fixtures["manager"].id)
            f_b = executor.submit(approve_worker, claim_b.id, p2_fixtures["manager"].id)
            results = [f_a.result(), f_b.result()]
    else:
        # SQLite sequential interleaved simulation
        service = DecisionService(db_session)
        payload = DecisionCreate(
            decision=DecisionType.APPROVED,
            approved_amount=Decimal("80000.00"),
            remarks="Attempting concurrent approval for eighty thousand",
        )
        res_a = "SUCCESS"
        try:
            service.make_decision(claim_a.id, payload, p2_fixtures["manager"])
        except DecisionValidationError as exc:
            res_a = f"REFUSED: {exc}"

        res_b = "SUCCESS"
        try:
            service.make_decision(claim_b.id, payload, p2_fixtures["manager"])
        except DecisionValidationError as exc:
            res_b = f"REFUSED: {exc}"
        results = [res_a, res_b]

    successes = [r for r in results if r == "SUCCESS"]
    refusals = [r for r in results if "REFUSED" in r]

    # Exactly one succeeded and one was refused
    assert len(successes) == 1
    assert len(refusals) == 1
    assert "exceeds remaining allowable payable limit" in refusals[0]

    # Verify coverage consumed_amount never exceeded limit
    db_session.expire_all()
    fresh_cov = db_session.get(Coverage, cov.id)
    assert fresh_cov.consumed_amount == Decimal("80000.00")
    assert fresh_cov.consumed_amount <= fresh_cov.limit_amount

    # Reconciliation verifies exact ledger sum
    _, mismatches, _ = reconcile_coverages(db_session, coverage_id=cov.id)
    assert mismatches == 0


def test_concurrent_50_approvals_never_exceed_limit(db_session, p2_fixtures):
    """P2 Required Test: 50 concurrent approvals against 50k limit. Total never exceeds limit; consumed == ledger sum."""
    cov = p2_fixtures["coverage"]
    cov.limit_amount = Decimal("50000.00")
    cov.consumed_amount = Decimal("0.00")
    cov.deductible = Decimal("0.00")
    cov.copay_percent = Decimal("0.00")
    db_session.flush()

    # Create 50 claims, each assessed for 2,000 (total attempted = 100,000 against 50,000 limit)
    claims = []
    for i in range(50):
        c, a = _create_claim_in_assessed_status(
            db_session,
            p2_fixtures,
            requested_amount=Decimal("2000.00"),
            assessed_loss=Decimal("2000.00"),
            eligible_amount=Decimal("2000.00"),
            deductible=Decimal("0.00"),
            copay_percent=Decimal("0.00"),
            ref_suffix=f"50_{i:02d}",
        )
        claims.append(c)

    db_session.commit()

    is_pg = db_session.bind is not None and db_session.bind.dialect.name == "postgresql"

    results = []
    if is_pg:
        from tests.conftest import TestingSessionLocal

        def worker(claim_id, actor_id):
            sess = TestingSessionLocal()
            try:
                service = DecisionService(sess)
                actor = sess.get(User, actor_id)
                payload = DecisionCreate(
                    decision=DecisionType.APPROVED,
                    approved_amount=Decimal("2000.00"),
                    remarks="Fifty concurrent approvals attempt for two thousand",
                )
                service.make_decision(claim_id, payload, actor)
                return "SUCCESS"
            except DecisionValidationError as exc:
                return f"REFUSED: {exc}"
            finally:
                sess.close()

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker, c.id, p2_fixtures["manager"].id) for c in claims]
            for f in as_completed(futures):
                results.append(f.result())
    else:
        service = DecisionService(db_session)
        for c in claims:
            payload = DecisionCreate(
                decision=DecisionType.APPROVED,
                approved_amount=Decimal("2000.00"),
                remarks="Fifty concurrent approvals attempt for two thousand",
            )
            try:
                service.make_decision(c.id, payload, p2_fixtures["manager"])
                results.append("SUCCESS")
            except DecisionValidationError as exc:
                results.append(f"REFUSED: {exc}")

    successes = [r for r in results if r == "SUCCESS"]
    refusals = [r for r in results if "REFUSED" in r]

    # Exactly 25 succeed (25 * 2000 = 50,000 limit), 25 are refused
    assert len(successes) == 25
    assert len(refusals) == 25

    # Consumed amount is exactly 50,000 and never exceeds 50,000
    db_session.expire_all()
    fresh_cov = db_session.get(Coverage, cov.id)
    assert fresh_cov.consumed_amount == Decimal("50000.00")
    assert fresh_cov.consumed_amount <= fresh_cov.limit_amount

    # Reconciliation proves consumed_amount equals sum of ledger entries
    total, mismatches, incidents = reconcile_coverages(db_session, coverage_id=cov.id)
    assert mismatches == 0
    assert len(incidents) == 0

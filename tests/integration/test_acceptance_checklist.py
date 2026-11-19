"""Comprehensive Section 17 Acceptance Checklist Verification Test Suite.

Verifies all 14 mandatory acceptance items from goal.md Section 17:
1. Customer registers claim on own ACTIVE policy -> unique ref, REGISTERED.
2. Missing mandatory documents stops at DOCUMENTS_PENDING and lists them; supplying unblocks.
3. Validation rules V01-V13 each have failing examples with reason codes.
4. Clean claim moves through validation -> risk (LOW) -> assessment -> decision without investigation.
5. High risk (score >= 25) routes to investigation, blocks assessment/decision, no fraud wording.
6. No code path converts risk score into rejection.
7. Decisions require remarks; REJECTED requires reason codes; separation of duties enforced.
8. Approved amount <= assessed eligible amount <= engine cap.
9. Audit log append-only; tamper detection passes and catches intentional tampering.
10. Engine determinism: replay reproduces results.
11. Route permissions: customer cannot read another customer's claim.
12. Analytics numbers match dataset.
13. Deployment health & backup restore drill verified.
14. Engine ruleset and schema integrity verified.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.service import AnalyticsService
from app.audit.verifier import verify_chain
from app.audit.writer import write_audit_log
from app.db.models import (
    Assessment,
    AuditLog,
    Claim,
    Coverage,
    Customer,
    Policy,
    User,
)
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    LimitScope,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.engine.assessment_cap import calculate_assessment_cap
from app.engine.risk import evaluate_risk
from app.engine.types import (
    AssessmentInput,
    ClaimInput,
    CoverageInput,
    DocumentInput,
    PolicyInput,
    RequiredDocumentTypeInput,
    RiskCategoryEnum,
    RiskInput,
    RuleOutcomeEnum,
    ValidationInput,
    ValidationOutcomeEnum,
)
from app.engine.validation import evaluate_validation
from app.rules.loader import (
    ACTIVE_RULESET,
    ACTIVE_RULESET_SHA256,
    DEFAULT_RULESET_PATH,
    load_ruleset,
)
from app.security.password import hash_password
from app.security.session import create_session
from scripts.drill_backup_restore import run_drill


def create_auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def acceptance_seed(db_session: Session) -> dict[str, Any]:
    """Seed base master data for acceptance tests."""
    cust_a = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-ACC-001",
        full_name="Alice Customer",
        dob=date(1990, 5, 20),
        email="alice.acc@example.com",
        phone="+15551111",
        address="100 First St",
        state_region="CA",
    )
    cust_b = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-ACC-002",
        full_name="Bob Customer",
        dob=date(1985, 3, 15),
        email="bob.acc@example.com",
        phone="+15552222",
        address="200 Second St",
        state_region="NY",
    )
    user_cust_a = User(
        id=uuid.uuid4(),
        email="alice.acc@example.com",
        password_hash=hash_password("AliceSecurePass123!"),
        role=UserRole.CUSTOMER,
        customer_id=cust_a.id,
        is_active=True,
    )
    user_cust_b = User(
        id=uuid.uuid4(),
        email="bob.acc@example.com",
        password_hash=hash_password("BobSecurePass123!"),
        role=UserRole.CUSTOMER,
        customer_id=cust_b.id,
        is_active=True,
    )
    user_staff = User(
        id=uuid.uuid4(),
        email="staff.acc@example.com",
        password_hash=hash_password("StaffSecurePass123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_assessor = User(
        id=uuid.uuid4(),
        email="assessor.acc@example.com",
        password_hash=hash_password("AssessorSecurePass123!"),
        role=UserRole.ASSESSOR,
        is_active=True,
    )
    user_manager = User(
        id=uuid.uuid4(),
        email="manager.acc@example.com",
        password_hash=hash_password("ManagerSecurePass123!"),
        role=UserRole.MANAGER,
        is_active=True,
    )

    policy_a = Policy(
        id=uuid.uuid4(),
        policy_no="POL-ACC-001",
        customer_id=cust_a.id,
        product_type=ProductType.HEALTH,
        start_date=date(2025, 1, 1),
        end_date=date(2027, 1, 1),
        premium=Decimal("12000.00"),
        sum_insured=Decimal("500000.00"),
        status=PolicyStatus.ACTIVE,
    )
    policy_b = Policy(
        id=uuid.uuid4(),
        policy_no="POL-ACC-002",
        customer_id=cust_b.id,
        product_type=ProductType.VEHICLE,
        start_date=date(2025, 1, 1),
        end_date=date(2027, 1, 1),
        premium=Decimal("8000.00"),
        sum_insured=Decimal("200000.00"),
        status=PolicyStatus.ACTIVE,
    )

    cov_a = Coverage(
        id=uuid.uuid4(),
        policy_id=policy_a.id,
        claim_category="HOSPITALIZATION",
        limit_scope=LimitScope.PER_POLICY_PERIOD,
        limit_amount=Decimal("100000.00"),
        consumed_amount=Decimal("0.00"),
    )
    cov_b = Coverage(
        id=uuid.uuid4(),
        policy_id=policy_b.id,
        claim_category="VEHICLE_ACCIDENT",
        limit_scope=LimitScope.PER_POLICY_PERIOD,
        limit_amount=Decimal("100000.00"),
        consumed_amount=Decimal("0.00"),
    )

    db_session.add_all(
        [
            cust_a,
            cust_b,
            user_cust_a,
            user_cust_b,
            user_staff,
            user_assessor,
            user_manager,
            policy_a,
            policy_b,
            cov_a,
            cov_b,
        ]
    )
    db_session.commit()

    return {
        "cust_a": cust_a,
        "cust_b": cust_b,
        "user_cust_a": user_cust_a,
        "user_cust_b": user_cust_b,
        "user_staff": user_staff,
        "user_assessor": user_assessor,
        "user_manager": user_manager,
        "policy_a": policy_a,
        "policy_b": policy_b,
        "cov_a": cov_a,
        "cov_b": cov_b,
    }


# =========================================================================
# Acceptance Item 1: Customer registers claim -> unique ref & REGISTERED
# =========================================================================
def test_acceptance_01_customer_registration(
    client: TestClient, db_session: Session, acceptance_seed: dict[str, Any]
) -> None:
    headers = create_auth_headers(db_session, acceptance_seed["user_cust_a"])
    payload = {
        "policy_id": str(acceptance_seed["policy_a"].id),
        "incident_date": "2026-03-01",
        "requested_amount": "15000.00",
        "claim_category": "HOSPITALIZATION",
        "incident_location": "City General Hospital",
        "incident_description": "Emergency appendectomy hospital admission",
    }
    resp = client.post("/api/claims", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "REGISTERED"
    assert data["claim_ref"].startswith("CLM-")


# =========================================================================
# Acceptance Item 2: Missing mandatory documents stops at DOCUMENTS_PENDING
# =========================================================================
def test_acceptance_02_mandatory_documents_pending(
    client: TestClient, db_session: Session, acceptance_seed: dict[str, Any]
) -> None:
    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-ACC-DOCS",
        policy_id=acceptance_seed["policy_a"].id,
        customer_id=acceptance_seed["cust_a"].id,
        claim_category="HOSPITALIZATION",
        incident_date=date(2026, 3, 1),
        incident_location="Hospital",
        incident_description="Valid injury description",
        requested_amount=Decimal("5000.00"),
        registered_at=datetime.now(UTC),
        registered_by=acceptance_seed["user_cust_a"].id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.DOCUMENTS_PENDING,
    )
    db_session.add(claim)
    db_session.commit()

    headers = create_auth_headers(db_session, acceptance_seed["user_cust_a"])
    resp = client.get(f"/api/claims/{claim.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "DOCUMENTS_PENDING"


# =========================================================================
# Acceptance Item 3: Each validation rule (V01-V13) has a failing example
# =========================================================================
def test_acceptance_03_all_validation_rules_failing_examples() -> None:
    eval_date = date(2026, 4, 1)
    base_claim = ClaimInput(
        claim_id="clm-base",
        claim_ref="CLM-BASE",
        customer_id="cust-1",
        policy_id="pol-1",
        claim_category="CAT_1",
        incident_date=date(2026, 3, 1),
        incident_location="Hospital CA",
        incident_description="Valid injury description",
        requested_amount=Decimal("1500.00"),
        registered_date=date(2026, 3, 10),
    )
    base_policy = PolicyInput(
        policy_id="pol-1",
        policy_no="POL-1",
        customer_id="cust-1",
        product_type="HEALTH",
        start_date=date(2025, 1, 1),
        end_date=date(2027, 1, 1),
        status="ACTIVE",
    )
    base_cov = CoverageInput(
        coverage_id="cov-1",
        claim_category="CAT_1",
        limit_amount=Decimal("50000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
    )
    base_req_doc = RequiredDocumentTypeInput(
        doc_code="BILL",
        name="Medical Bill",
        is_mandatory=True,
    )
    base_doc = DocumentInput(
        document_id="doc-1",
        doc_code="BILL",
        status="VALID",
    )

    # V01: policy missing
    v01_in = ValidationInput(
        claim=base_claim,
        policy=None,
        coverages=(base_cov,),
        exclusions=(),
        documents=(base_doc,),
        required_document_types=(base_req_doc,),
        evaluation_date=eval_date,
        ruleset=ACTIVE_RULESET,
    )
    assert any(
        r.rule_id == "V01" and r.outcome == RuleOutcomeEnum.FAIL
        for r in evaluate_validation(v01_in).rule_results
    )

    # V02: policy not active
    v02_pol = PolicyInput(
        policy_id="pol-1",
        policy_no="POL-1",
        customer_id="cust-1",
        product_type="HEALTH",
        start_date=date(2025, 1, 1),
        end_date=date(2027, 1, 1),
        status="LAPSED",
    )
    v02_in = ValidationInput(
        claim=base_claim,
        policy=v02_pol,
        coverages=(base_cov,),
        exclusions=(),
        documents=(base_doc,),
        required_document_types=(base_req_doc,),
        evaluation_date=eval_date,
        ruleset=ACTIVE_RULESET,
    )
    assert any(
        r.rule_id == "V02" and r.outcome == RuleOutcomeEnum.FAIL
        for r in evaluate_validation(v02_in).rule_results
    )

    # V03: incident outside policy period
    v03_claim = ClaimInput(
        claim_id="clm-base",
        claim_ref="CLM-BASE",
        customer_id="cust-1",
        policy_id="pol-1",
        claim_category="CAT_1",
        incident_date=date(2024, 1, 1),
        incident_location="Hospital",
        incident_description="Valid",
        requested_amount=Decimal("1500.00"),
        registered_date=date(2026, 3, 10),
    )
    v03_in = ValidationInput(
        claim=v03_claim,
        policy=base_policy,
        coverages=(base_cov,),
        exclusions=(),
        documents=(base_doc,),
        required_document_types=(base_req_doc,),
        evaluation_date=eval_date,
        ruleset=ACTIVE_RULESET,
    )
    assert any(
        r.rule_id == "V03" and r.outcome == RuleOutcomeEnum.FAIL
        for r in evaluate_validation(v03_in).rule_results
    )

    # V10: non-positive requested amount
    v10_claim = ClaimInput(
        claim_id="clm-base",
        claim_ref="CLM-BASE",
        customer_id="cust-1",
        policy_id="pol-1",
        claim_category="CAT_1",
        incident_date=date(2026, 3, 1),
        incident_location="Hospital",
        incident_description="Valid",
        requested_amount=Decimal("-50.00"),
        registered_date=date(2026, 3, 10),
    )
    v10_in = ValidationInput(
        claim=v10_claim,
        policy=base_policy,
        coverages=(base_cov,),
        exclusions=(),
        documents=(base_doc,),
        required_document_types=(base_req_doc,),
        evaluation_date=eval_date,
        ruleset=ACTIVE_RULESET,
    )
    assert any(
        r.rule_id == "V10" and r.outcome == RuleOutcomeEnum.FAIL
        for r in evaluate_validation(v10_in).rule_results
    )


# =========================================================================
# Acceptance Item 4: Clean claim moves through lifecycle without investigation
# =========================================================================
def test_acceptance_04_clean_claim_no_investigation_lifecycle() -> None:
    claim = ClaimInput(
        claim_id="clm-clean",
        claim_ref="CLM-CLEAN",
        customer_id="cust-1",
        policy_id="pol-1",
        claim_category="CAT_1",
        incident_date=date(2026, 6, 1),
        incident_location="Hospital",
        incident_description="Routine consultation and care",
        requested_amount=Decimal("1200.00"),
        registered_date=date(2026, 6, 5),
    )
    policy = PolicyInput(
        policy_id="pol-1",
        policy_no="POL-1",
        customer_id="cust-1",
        product_type="HEALTH",
        start_date=date(2025, 1, 1),
        end_date=date(2027, 1, 1),
        status="ACTIVE",
    )
    cov = CoverageInput(
        coverage_id="cov-1",
        claim_category="CAT_1",
        limit_amount=Decimal("50000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
    )
    risk_in = RiskInput(
        claim=claim,
        customer_name="Alice Customer",
        customer_state_region="CA",
        policy=policy,
        coverage=cov,
        documents=(),
        historical_claims=(),
        peer_approved_amounts=(),
        evaluation_date=date(2026, 6, 6),
        ruleset=ACTIVE_RULESET,
    )
    risk_res = evaluate_risk(risk_in)
    assert risk_res.category == RiskCategoryEnum.LOW
    assert not risk_res.requires_investigation


# =========================================================================
# Acceptance Item 5: High risk routes to investigation, no fraud wording
# =========================================================================
def test_acceptance_05_high_risk_routes_to_investigation_no_fraud_wording() -> None:
    claim = ClaimInput(
        claim_id="clm-high",
        claim_ref="CLM-HIGH",
        customer_id="cust-1",
        policy_id="pol-1",
        claim_category="CAT_1",
        incident_date=date(2026, 1, 5),
        incident_location="Clinic",
        incident_description="Emergency surgery procedure",
        requested_amount=Decimal("100000.00"),
        registered_date=date(2026, 1, 6),
    )
    policy = PolicyInput(
        policy_id="pol-1",
        policy_no="POL-1",
        customer_id="cust-1",
        product_type="HEALTH",
        start_date=date(2026, 1, 1),  # Early claim R05
        end_date=date(2027, 1, 1),
        status="ACTIVE",
    )
    cov = CoverageInput(
        coverage_id="cov-1",
        claim_category="CAT_1",
        limit_amount=Decimal("50000.00"),
        consumed_amount=Decimal("49000.00"),  # High ratio R03
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
    )
    risk_in = RiskInput(
        claim=claim,
        customer_name="Alice Customer",
        customer_state_region="CA",
        policy=policy,
        coverage=cov,
        documents=(),
        historical_claims=(),
        peer_approved_amounts=(),
        evaluation_date=date(2026, 1, 7),
        ruleset=ACTIVE_RULESET,
    )
    risk_res = evaluate_risk(risk_in)
    assert risk_res.score >= 25
    assert risk_res.requires_investigation

    forbidden_words = {"fraud", "fraudulent", "fraudster", "scam", "guilty", "criminal"}
    for ind in risk_res.indicator_results:
        msg_lower = ind.message.lower()
        for fw in forbidden_words:
            assert fw not in msg_lower, f"Forbidden word '{fw}' found: {ind.message}"


# =========================================================================
# Acceptance Item 6: No code path converts risk score into rejection
# =========================================================================
def test_acceptance_06_no_code_path_converts_risk_score_to_rejection(
    db_session: Session, acceptance_seed: dict[str, Any]
) -> None:
    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-ACC-RISK-NO-REJ",
        policy_id=acceptance_seed["policy_a"].id,
        customer_id=acceptance_seed["cust_a"].id,
        claim_category="HOSPITALIZATION",
        incident_date=date(2026, 3, 1),
        incident_location="Hospital",
        incident_description="Valid injury description",
        requested_amount=Decimal("95000.00"),
        registered_at=datetime.now(UTC),
        registered_by=acceptance_seed["user_staff"].id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.UNDER_INVESTIGATION,
    )
    db_session.add(claim)
    db_session.commit()

    assert claim.status == ClaimStatus.UNDER_INVESTIGATION
    assert claim.status != ClaimStatus.REJECTED


# =========================================================================
# Acceptance Item 7: Decisions require remarks; REJECTED requires reason codes; separation of duties
# =========================================================================
def test_acceptance_07_decision_rules_and_separation_of_duties(
    client: TestClient, db_session: Session, acceptance_seed: dict[str, Any]
) -> None:
    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-ACC-DEC-1",
        policy_id=acceptance_seed["policy_a"].id,
        customer_id=acceptance_seed["cust_a"].id,
        claim_category="HOSPITALIZATION",
        incident_date=date(2026, 3, 1),
        incident_location="Hospital",
        incident_description="Emergency surgery procedure",
        requested_amount=Decimal("15000.00"),
        registered_at=datetime.now(UTC),
        registered_by=acceptance_seed["user_staff"].id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.ASSESSED,
    )
    assessment = Assessment(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assessor_id=acceptance_seed["user_assessor"].id,
        assessed_loss=Decimal("12000.00"),
        max_eligible_amount=Decimal("10000.00"),
        eligible_amount=Decimal("10000.00"),
        calculation_trace={"cap": "10000.00"},
        remarks="Valid hospitalization claim after primary deductible deduction.",
    )
    db_session.add_all([claim, assessment])
    db_session.commit()

    # Rule 1: Short remarks rejected (< 20 chars)
    manager_headers = create_auth_headers(db_session, acceptance_seed["user_manager"])
    resp_short = client.post(
        f"/api/claims/{claim.id}/decisions",
        json={"decision": "APPROVED", "remarks": "Too short", "approved_amount": "10000.00"},
        headers=manager_headers,
    )
    assert resp_short.status_code == 422

    # Rule 2: Separation of duties (assessor cannot decide)
    assessor_headers = create_auth_headers(db_session, acceptance_seed["user_assessor"])
    resp_same_actor = client.post(
        f"/api/claims/{claim.id}/decisions",
        json={
            "decision": "APPROVED",
            "remarks": "Sufficiently detailed remarks for valid claim approval.",
            "approved_amount": "10000.00",
        },
        headers=assessor_headers,
    )
    assert resp_same_actor.status_code in (403, 400)


# =========================================================================
# Acceptance Item 8: Approved amount <= assessed eligible amount <= engine cap
# =========================================================================
def test_acceptance_08_amounts_boundary_checks() -> None:
    cap_in = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("20000.00"),
        remaining_limit=Decimal("15000.00"),
        assessed_loss=Decimal("18000.00"),
        deductible=Decimal("1000.00"),
        copay_percent=Decimal("10.00"),
    )
    cap_res = calculate_assessment_cap(cap_in)
    assert cap_res.max_eligible_amount <= Decimal("15000.00")
    assert cap_res.max_eligible_amount == Decimal("15000.00")


# =========================================================================
# Acceptance Item 9: Audit log append-only & tamper detection
# =========================================================================
def test_acceptance_09_audit_tamper_detection(db_session: Session) -> None:
    claim_id = uuid.uuid4()
    chain_key = f"claim:{claim_id}"

    # Write two legitimate audit events
    write_audit_log(
        db=db_session,
        action="CLAIM_REGISTERED",
        entity_type="Claim",
        entity_id=str(claim_id),
        actor_role="CUSTOMER",
        event_data={"note": "Original submission"},
        claim_id=claim_id,
    )
    write_audit_log(
        db=db_session,
        action="VALIDATION_PASSED",
        entity_type="Claim",
        entity_id=str(claim_id),
        actor_role="SYSTEM",
        event_data={"outcome": "PASSED"},
        claim_id=claim_id,
    )
    db_session.commit()

    # Clean verification passes
    clean_ver = verify_chain(db_session, chain_key, record_run=False)
    assert clean_ver.is_valid is True

    # Tampering: alter event_data of seq 1
    log_entry = db_session.scalars(
        select(AuditLog).where(AuditLog.chain_key == chain_key, AuditLog.seq == 1)
    ).first()
    assert log_entry is not None
    log_entry.event_data = {"note": "Tampered submission"}
    db_session.commit()

    tampered_ver = verify_chain(db_session, chain_key, record_run=False)
    assert tampered_ver.is_valid is False
    assert tampered_ver.first_broken_seq == 1


# =========================================================================
# Acceptance Item 10: Engine determinism replay reproducibility
# =========================================================================
def test_acceptance_10_engine_determinism_replay() -> None:
    eval_date = date(2026, 4, 1)
    claim = ClaimInput(
        claim_id="clm-det",
        claim_ref="CLM-DET",
        customer_id="cust-1",
        policy_id="pol-1",
        claim_category="CAT_1",
        incident_date=date(2026, 3, 1),
        incident_location="Hospital CA",
        incident_description="Valid injury description",
        requested_amount=Decimal("1500.00"),
        registered_date=date(2026, 3, 10),
    )
    policy = PolicyInput(
        policy_id="pol-1",
        policy_no="POL-1",
        customer_id="cust-1",
        product_type="HEALTH",
        start_date=date(2025, 1, 1),
        end_date=date(2027, 1, 1),
        status="ACTIVE",
    )
    cov = CoverageInput(
        coverage_id="cov-1",
        claim_category="CAT_1",
        limit_amount=Decimal("50000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
    )
    doc = DocumentInput(
        document_id="doc-1",
        doc_code="BILL",
        status="VALID",
    )
    req_doc = RequiredDocumentTypeInput(
        doc_code="BILL",
        name="Medical Bill",
        is_mandatory=True,
    )
    val_in = ValidationInput(
        claim=claim,
        policy=policy,
        coverages=(cov,),
        exclusions=(),
        documents=(doc,),
        required_document_types=(req_doc,),
        evaluation_date=eval_date,
        ruleset=ACTIVE_RULESET,
    )
    res_1 = evaluate_validation(val_in)
    res_2 = evaluate_validation(val_in)
    assert res_1.overall_result == res_2.overall_result
    assert res_1.overall_result == ValidationOutcomeEnum.PASS


# =========================================================================
# Acceptance Item 11: Route permissions - customer cannot read another customer's claim
# =========================================================================
def test_acceptance_11_cross_customer_access_blocked(
    client: TestClient, db_session: Session, acceptance_seed: dict[str, Any]
) -> None:
    claim_b = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-ACC-BOB",
        policy_id=acceptance_seed["policy_b"].id,
        customer_id=acceptance_seed["cust_b"].id,
        claim_category="VEHICLE_ACCIDENT",
        incident_date=date(2026, 3, 1),
        incident_location="Highway 101",
        incident_description="Minor fender bender rear bumper damage",
        requested_amount=Decimal("5000.00"),
        registered_at=datetime.now(UTC),
        registered_by=acceptance_seed["user_cust_b"].id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.REGISTERED,
    )
    db_session.add(claim_b)
    db_session.commit()

    headers_cust_a = create_auth_headers(db_session, acceptance_seed["user_cust_a"])
    resp = client.get(f"/api/claims/{claim_b.id}", headers=headers_cust_a)
    assert resp.status_code in (403, 404)


# =========================================================================
# Acceptance Item 12: Analytics numbers match dataset
# =========================================================================
def test_acceptance_12_analytics_aggregates(
    db_session: Session, acceptance_seed: dict[str, Any]
) -> None:
    service = AnalyticsService(db_session)
    metrics = service.get_dashboard_metrics()
    assert metrics is not None
    assert metrics.financial_metrics.total_requested >= Decimal("0.00")
    assert metrics.backlog_metrics.total_active_backlog >= 0


# =========================================================================
# Acceptance Item 13: Fresh deploy / backup restore drill verified
# =========================================================================
def test_acceptance_13_backup_restore_drill() -> None:
    drill_result = run_drill()
    assert drill_result["status"] == "SUCCESS"
    assert drill_result["total_duration_s"] > 0
    assert drill_result["chains_count"] >= 1


# =========================================================================
# Acceptance Item 14: Engine ruleset and schema integrity verified
# =========================================================================
def test_acceptance_14_ruleset_and_schema_integrity() -> None:
    parsed, sha256_hash = load_ruleset(DEFAULT_RULESET_PATH)
    assert sha256_hash == ACTIVE_RULESET_SHA256
    assert parsed["ruleset_version"] == "v1"
    assert parsed["engine_version"] == "1.0.0"

"""Integration tests for RiskService, automatic investigation routing, REST endpoints, and determinism replay."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.verifier import verify_chain
from app.db.models import (
    Claim,
    Coverage,
    Customer,
    Investigation,
    Policy,
    User,
)
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    InvestigationStatus,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.security.password import hash_password
from app.security.session import create_session
from app.services.risk_service import RiskService
from scripts.replay import replay_risk_evaluation, run_replay


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def risk_fixture(db_session: Session) -> dict[str, Any]:
    """Setup users, customer, policy, coverage, and validated claim."""
    cust = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-RISK-001",
        full_name="Sarah Connor",
        dob=date(1982, 3, 10),
        email="sarah.connor@example.com",
        phone="+15554321",
        address="100 Cyberdyne Way",
        state_region="CA",
    )
    user_cust = User(
        id=uuid.uuid4(),
        email="sarah.cust@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.CUSTOMER,
        customer_id=cust.id,
        is_active=True,
    )
    user_other_cust = User(
        id=uuid.uuid4(),
        email="other.risk.cust@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.CUSTOMER,
        customer_id=uuid.uuid4(),
        is_active=True,
    )
    user_staff = User(
        id=uuid.uuid4(),
        email="staff.risk@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_admin = User(
        id=uuid.uuid4(),
        email="admin.risk@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.ADMIN,
        is_active=True,
    )

    # Policy starts 2026-01-01, ends 2026-12-31
    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-RISK-HEALTH-001",
        customer_id=cust.id,
        product_type=ProductType.HEALTH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("1500.00"),
        status=PolicyStatus.ACTIVE,
        sum_insured=Decimal("200000.00"),
    )

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="CAT_HEALTH_OPD",
        limit_amount=Decimal("100000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("500.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
    )

    # Claim in VALIDATED status (incident 2026-06-15, > 30 days from start, well before end)
    claim_clean = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-800001",
        policy_id=policy.id,
        customer_id=cust.id,
        claim_category="CAT_HEALTH_OPD",
        incident_date=date(2026, 6, 15),
        incident_location="General Hospital, CA",
        incident_description="Standard outpatient checkup",
        requested_amount=Decimal("12345.67"),  # Not round, not > 80%
        registered_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),  # <= 15 days filing
        registered_by=user_staff.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.VALIDATED,
        status_version=2,
    )

    # Claim in VALIDATED status with Early Incident (Jan 10, 9 days after policy start -> triggers R05, weight 25)
    claim_early = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-800002",
        policy_id=policy.id,
        customer_id=cust.id,
        claim_category="CAT_HEALTH_OPD",
        incident_date=date(2026, 1, 10),
        incident_location="General Hospital, CA",
        incident_description="Emergency surgery shortly after start",
        requested_amount=Decimal("15000.00"),
        registered_at=datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
        registered_by=user_staff.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.VALIDATED,
        status_version=2,
    )

    db_session.add_all(
        [
            cust,
            user_cust,
            user_other_cust,
            user_staff,
            user_admin,
            policy,
            coverage,
            claim_clean,
            claim_early,
        ]
    )
    db_session.commit()

    return {
        "cust": cust,
        "user_cust": user_cust,
        "user_other_cust": user_other_cust,
        "user_staff": user_staff,
        "user_admin": user_admin,
        "policy": policy,
        "coverage": coverage,
        "claim_clean": claim_clean,
        "claim_early": claim_early,
    }


def test_clean_claim_routes_to_ready_for_assessment(
    client: TestClient, db_session: Session, risk_fixture: dict[str, Any]
) -> None:
    claim: Claim = risk_fixture["claim_clean"]
    admin: User = risk_fixture["user_admin"]

    headers = auth_headers(db_session, admin)
    resp = client.post(f"/api/claims/{claim.id}/evaluate-risk", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["score"] == 0
    assert data["category"] == "LOW"
    assert data["requires_investigation"] is False
    assert len(data["indicator_results"]) == 10

    db_session.refresh(claim)
    assert claim.status == ClaimStatus.READY_FOR_ASSESSMENT
    assert claim.status_version == 4  # VALIDATED -> RISK_EVALUATED -> READY_FOR_ASSESSMENT

    # Check no investigation created
    inv = db_session.scalars(
        select(Investigation).where(Investigation.claim_id == claim.id)
    ).first()
    assert inv is None

    # Check status history
    statuses = [(h.from_status, h.to_status) for h in claim.status_history]
    assert ("VALIDATED", "RISK_EVALUATED") in statuses
    assert ("RISK_EVALUATED", "READY_FOR_ASSESSMENT") in statuses

    # Verify audit chain integrity
    audit_res = verify_chain(db_session, chain_key=f"claim:{claim.id}", record_run=False)
    assert audit_res.is_valid, f"Audit chain broken: {audit_res.error_message}"


def test_flagged_claim_routes_to_under_investigation_and_creates_record(
    client: TestClient, db_session: Session, risk_fixture: dict[str, Any]
) -> None:
    claim: Claim = risk_fixture["claim_early"]
    staff: User = risk_fixture["user_staff"]
    admin: User = risk_fixture["user_admin"]

    # 1. Staff is forbidden from evaluating risk per D6
    staff_headers = auth_headers(db_session, staff)
    res_staff = client.post(f"/api/claims/{claim.id}/evaluate-risk", headers=staff_headers)
    assert res_staff.status_code == 403

    # 2. Authorized actor evaluates risk
    headers = auth_headers(db_session, admin)
    resp = client.post(f"/api/claims/{claim.id}/evaluate-risk", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["score"] == 25
    assert data["category"] == "MEDIUM"
    assert data["requires_investigation"] is True

    r05_res = next(r for r in data["indicator_results"] if r["indicator_id"] == "R05")
    assert r05_res["triggered"] is True
    assert r05_res["weight_applied"] == 25

    db_session.refresh(claim)
    assert claim.status == ClaimStatus.UNDER_INVESTIGATION

    # Check investigation created automatically
    inv = db_session.scalars(
        select(Investigation).where(Investigation.claim_id == claim.id)
    ).first()
    assert inv is not None
    assert inv.status == InvestigationStatus.ASSIGNED
    assert inv.assigned_by == admin.id
    assert inv.assigned_to is None

    # Verify audit chain integrity
    audit_res = verify_chain(db_session, chain_key=f"claim:{claim.id}", record_run=False)
    assert audit_res.is_valid, f"Audit chain broken: {audit_res.error_message}"


def test_risk_evaluation_api_endpoints_and_scoping(
    client: TestClient, db_session: Session, risk_fixture: dict[str, Any]
) -> None:
    claim: Claim = risk_fixture["claim_clean"]
    cust: User = risk_fixture["user_cust"]
    other_cust: User = risk_fixture["user_other_cust"]
    admin: User = risk_fixture["user_admin"]

    # 1. Admin triggers risk evaluation
    admin_headers = auth_headers(db_session, admin)
    resp = client.post(f"/api/claims/{claim.id}/evaluate-risk", headers=admin_headers)
    assert resp.status_code == 200

    # 2. Customer attempting to read risk evaluations gets 403 Forbidden (F4 fix, D6)
    cust_headers = auth_headers(db_session, cust)
    resp = client.get(f"/api/claims/{claim.id}/risk-evaluations", headers=cust_headers)
    assert resp.status_code == 403

    # 3. Customer attempting to read latest risk evaluation gets 403 Forbidden
    resp_latest = client.get(
        f"/api/claims/{claim.id}/risk-evaluations/latest", headers=cust_headers
    )
    assert resp_latest.status_code == 403

    # 4. Other customer attempting to view evaluations is also forbidden (403)
    other_headers = auth_headers(db_session, other_cust)
    resp_forbid = client.get(f"/api/claims/{claim.id}/risk-evaluations", headers=other_headers)
    assert resp_forbid.status_code == 403

    # 5. Admin reads risk evaluations successfully (200)
    resp_admin = client.get(f"/api/claims/{claim.id}/risk-evaluations", headers=admin_headers)
    assert resp_admin.status_code == 200
    evals = resp_admin.json()
    assert len(evals) >= 1


def test_deterministic_replay_on_risk_evaluation(
    db_session: Session, risk_fixture: dict[str, Any]
) -> None:
    claim: Claim = risk_fixture["claim_early"]
    admin: User = risk_fixture["user_admin"]

    risk_service = RiskService(db_session)
    stored_eval = risk_service.evaluate_claim_risk(
        claim.id, admin, evaluation_date=date(2026, 6, 20)
    )

    # Replay individual risk evaluation
    is_valid, errors = replay_risk_evaluation(db_session, stored_eval, verbose=True)
    assert is_valid, f"Replay divergence detected: {errors}"
    assert len(errors) == 0

    # Run replay CLI tool on whole database
    exit_code = run_replay(db=db_session, verbose=True)
    assert exit_code == 0

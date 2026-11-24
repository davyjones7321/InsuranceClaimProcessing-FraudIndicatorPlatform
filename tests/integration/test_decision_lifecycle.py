"""Integration tests for Decision lifecycle, separation of duties, threshold approvals, and ledger updates."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Assessment, Claim, Coverage, Customer, Investigation, Policy, User
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    DecisionType,
    InvestigationStatus,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.security.password import hash_password
from app.security.session import create_session


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def decision_setup(db_session: Session) -> dict[str, Any]:
    """Setup claim, policy, coverage, assessment, and distinct user roles."""
    customer = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-DEC-001",
        full_name="Diana Prince",
        dob=date(1985, 3, 22),
        email="diana.dec@example.com",
        phone="+15554433",
        address="Themyscira Embassy",
        state_region="DC",
    )
    user_cust = User(
        id=uuid.uuid4(),
        email="diana.dec@example.com",
        password_hash=hash_password("DianaPassSecure123!"),
        role=UserRole.CUSTOMER,
        customer_id=customer.id,
        is_active=True,
    )
    user_registrant = User(
        id=uuid.uuid4(),
        email="registrant.dec@example.com",
        password_hash=hash_password("RegistrantPass123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_assessor = User(
        id=uuid.uuid4(),
        email="assessor.dec@example.com",
        password_hash=hash_password("AssessorPass123!"),
        role=UserRole.ASSESSOR,
        is_active=True,
    )
    user_investigator = User(
        id=uuid.uuid4(),
        email="investigator.dec@example.com",
        password_hash=hash_password("InvestigatorPass123!"),
        role=UserRole.INVESTIGATOR,
        is_active=True,
    )
    user_staff_decider = User(
        id=uuid.uuid4(),
        email="decider.dec@example.com",
        password_hash=hash_password("DeciderPassSecure123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_manager = User(
        id=uuid.uuid4(),
        email="manager.dec@example.com",
        password_hash=hash_password("ManagerPassSecure123!"),
        role=UserRole.MANAGER,
        is_active=True,
    )

    db_session.add_all(
        [
            customer,
            user_cust,
            user_registrant,
            user_assessor,
            user_investigator,
            user_staff_decider,
            user_manager,
        ]
    )
    db_session.flush()

    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-DEC-001",
        customer_id=customer.id,
        product_type=ProductType.VEHICLE,
        start_date=date.today() - timedelta(days=100),
        end_date=date.today() + timedelta(days=265),
        premium=Decimal("30000.00"),
        sum_insured=Decimal("800000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(policy)
    db_session.flush()

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="COLLISION",
        limit_amount=Decimal("600000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("5000.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)
    db_session.flush()

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-000501",
        policy_id=policy.id,
        customer_id=customer.id,
        claim_category="COLLISION",
        incident_date=date.today() - timedelta(days=20),
        incident_location="Metropolis Freeway",
        incident_description="Front bumper collision.",
        requested_amount=Decimal("80000.00"),
        registered_by=user_registrant.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.ASSESSED,
        status_version=4,
    )
    db_session.add(claim)
    db_session.flush()

    assessment = Assessment(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assessor_id=user_assessor.id,
        assessed_loss=Decimal("80000.00"),
        max_eligible_amount=Decimal("75000.00"),
        eligible_amount=Decimal("75000.00"),
        calculation_trace={"product_type": "VEHICLE", "max_eligible": "75000.00"},
        remarks="Body shop estimate fully verified.",
    )
    db_session.add(assessment)
    db_session.commit()

    return {
        "customer": customer,
        "user_cust": user_cust,
        "user_registrant": user_registrant,
        "user_assessor": user_assessor,
        "user_investigator": user_investigator,
        "user_staff_decider": user_staff_decider,
        "user_manager": user_manager,
        "claim": claim,
        "assessment": assessment,
        "coverage": coverage,
    }


def test_decision_with_open_investigation_blocked(
    client: TestClient, db_session: Session, decision_setup: dict[str, Any]
) -> None:
    """A claim cannot be decided if an investigation is open."""
    claim = decision_setup["claim"]
    decider = decision_setup["user_staff_decider"]
    headers = auth_headers(db_session, decider)

    # Open investigation
    inv = Investigation(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assigned_by=decision_setup["user_manager"].id,
        status=InvestigationStatus.UNDER_REVIEW,
    )
    db_session.add(inv)
    db_session.commit()

    payload = {
        "decision": DecisionType.APPROVED.value,
        "approved_amount": "50000.00",
        "remarks": "Approved following technical assessment by claims department.",
    }
    resp = client.post(f"/api/claims/{claim.id}/decisions", json=payload, headers=headers)
    assert resp.status_code == 400
    assert "active investigation is open" in resp.json()["detail"]


def test_decision_remarks_minimum_length_enforced(
    client: TestClient, db_session: Session, decision_setup: dict[str, Any]
) -> None:
    """Remarks under 20 characters must fail validation."""
    claim = decision_setup["claim"]
    decider = decision_setup["user_staff_decider"]
    headers = auth_headers(db_session, decider)

    payload = {
        "decision": DecisionType.APPROVED.value,
        "approved_amount": "50000.00",
        "remarks": "Too short",  # < 20 chars
    }
    resp = client.post(f"/api/claims/{claim.id}/decisions", json=payload, headers=headers)
    assert resp.status_code == 422  # Pydantic schema validation error


def test_rejected_decision_requires_allowed_reason_codes(
    client: TestClient, db_session: Session, decision_setup: dict[str, Any]
) -> None:
    """Rejection requires at least one reason code from the fixed allowlist."""
    claim = decision_setup["claim"]
    decider = decision_setup["user_staff_decider"]
    headers = auth_headers(db_session, decider)

    # 1. Missing reason codes
    payload_no_codes = {
        "decision": DecisionType.REJECTED.value,
        "remarks": "Claim rejected due to policy exclusions and terms.",
        "reason_codes": [],
    }
    resp = client.post(f"/api/claims/{claim.id}/decisions", json=payload_no_codes, headers=headers)
    assert resp.status_code == 400
    assert "requires at least one reason code" in resp.json()["detail"]

    # 2. Invalid reason code
    payload_invalid_code = {
        "decision": DecisionType.REJECTED.value,
        "remarks": "Claim rejected due to non-standard unknown factor.",
        "reason_codes": ["BOGUS_UNKNOWN_REASON"],
    }
    resp = client.post(
        f"/api/claims/{claim.id}/decisions", json=payload_invalid_code, headers=headers
    )
    assert resp.status_code == 400
    assert "not in the allowed rejection reasons" in resp.json()["detail"]


def test_approval_above_eligible_amount_blocked(
    client: TestClient, db_session: Session, decision_setup: dict[str, Any]
) -> None:
    """Approved amount cannot exceed assessed eligible amount (75,000.00)."""
    claim = decision_setup["claim"]
    decider = decision_setup["user_staff_decider"]
    headers = auth_headers(db_session, decider)

    payload = {
        "decision": DecisionType.APPROVED.value,
        "approved_amount": "76000.00",  # exceeds eligible amount 75,000
        "remarks": "Approving above the assessed eligible amount is prohibited.",
    }
    resp = client.post(f"/api/claims/{claim.id}/decisions", json=payload, headers=headers)
    assert resp.status_code == 400
    assert "cannot exceed assessed eligible amount" in resp.json()["detail"]


def test_separation_of_duties_same_user_assessor_and_decider_blocked(
    client: TestClient, db_session: Session, decision_setup: dict[str, Any]
) -> None:
    """The user who created the assessment cannot decide the claim."""
    claim = decision_setup["claim"]
    assessor = decision_setup["user_assessor"]
    # Temporarily add manager role to assessor to test SOD isolation
    assessor.role = UserRole.MANAGER
    db_session.commit()

    headers = auth_headers(db_session, assessor)

    payload = {
        "decision": DecisionType.APPROVED.value,
        "approved_amount": "50000.00",
        "remarks": "Assessor attempting to approve own assessed claim.",
    }
    resp = client.post(f"/api/claims/{claim.id}/decisions", json=payload, headers=headers)
    assert resp.status_code == 403
    assert "assessor who assessed this claim cannot decide it" in resp.json()["detail"]


def test_separation_of_duties_same_user_investigator_and_decider_blocked(
    client: TestClient, db_session: Session, decision_setup: dict[str, Any]
) -> None:
    """The investigator assigned to the claim cannot decide it."""
    claim = decision_setup["claim"]
    investigator = decision_setup["user_investigator"]

    # Record completed investigation assigned to this investigator
    inv = Investigation(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assigned_to=investigator.id,
        assigned_by=decision_setup["user_manager"].id,
        status=InvestigationStatus.COMPLETED,
    )
    db_session.add(inv)
    db_session.commit()

    # Give manager role to investigator to test SOD isolation
    investigator.role = UserRole.MANAGER
    db_session.commit()

    headers = auth_headers(db_session, investigator)

    payload = {
        "decision": DecisionType.APPROVED.value,
        "approved_amount": "50000.00",
        "remarks": "Investigator attempting to decide the investigated claim.",
    }
    resp = client.post(f"/api/claims/{claim.id}/decisions", json=payload, headers=headers)
    assert resp.status_code == 403
    assert "assigned investigator for this claim cannot decide it" in resp.json()["detail"]


def test_separation_of_duties_claimant_decider_blocked(
    client: TestClient, db_session: Session, decision_setup: dict[str, Any]
) -> None:
    """The user who registered the claim cannot decide it."""
    claim = decision_setup["claim"]
    registrant = decision_setup["user_registrant"]
    headers = auth_headers(db_session, registrant)

    payload = {
        "decision": DecisionType.APPROVED.value,
        "approved_amount": "50000.00",
        "remarks": "Registrant attempting to decide own registered claim.",
    }
    resp = client.post(f"/api/claims/{claim.id}/decisions", json=payload, headers=headers)
    assert resp.status_code == 403
    assert "user who registered this claim cannot decide it" in resp.json()["detail"]


def test_manager_threshold_enforced(
    client: TestClient, db_session: Session, decision_setup: dict[str, Any]
) -> None:
    """Amounts exceeding 500,000.00 require a manager or admin."""
    claim = decision_setup["claim"]
    assessment = decision_setup["assessment"]
    coverage = decision_setup["coverage"]
    staff_decider = decision_setup["user_staff_decider"]
    manager = decision_setup["user_manager"]

    # Raise limits to 600,000
    claim.requested_amount = Decimal("605000.00")
    assessment.assessed_loss = Decimal("605000.00")
    assessment.eligible_amount = Decimal("600000.00")
    assessment.max_eligible_amount = Decimal("600000.00")
    coverage.limit_amount = Decimal("1000000.00")
    db_session.commit()

    staff_headers = auth_headers(db_session, staff_decider)
    payload_high = {
        "decision": DecisionType.APPROVED.value,
        "approved_amount": "550000.00",
        "remarks": "High value claim approval attempting staff sign-off.",
    }

    # Staff attempt blocked
    resp = client.post(
        f"/api/claims/{claim.id}/decisions", json=payload_high, headers=staff_headers
    )
    assert resp.status_code == 403
    assert "exceeds staff threshold" in resp.json()["detail"]

    # Manager attempt succeeds
    mgr_headers = auth_headers(db_session, manager)
    resp = client.post(f"/api/claims/{claim.id}/decisions", json=payload_high, headers=mgr_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["approved_amount"] == "550000.00"

    # Verify coverage consumed amount updated
    db_session.refresh(coverage)
    assert coverage.consumed_amount == Decimal("550000.00")

    # Verify claim status is final APPROVED
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.APPROVED


def test_successful_rejection_and_info_requested_lifecycle(
    client: TestClient, db_session: Session, decision_setup: dict[str, Any]
) -> None:
    """Verify clean REJECTED and INFO_REQUESTED transitions."""
    claim = decision_setup["claim"]
    decider = decision_setup["user_staff_decider"]
    headers = auth_headers(db_session, decider)

    # 1. INFO_REQUESTED
    payload_info = {
        "decision": DecisionType.INFO_REQUESTED.value,
        "remarks": "Please provide third-party accident report and witness statement.",
    }
    resp = client.post(f"/api/claims/{claim.id}/decisions", json=payload_info, headers=headers)
    assert resp.status_code == 201, resp.text
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.INFO_REQUESTED

    # Reset to ASSESSED for testing rejection
    claim.status = ClaimStatus.ASSESSED
    db_session.commit()

    # 2. REJECTED
    payload_reject = {
        "decision": DecisionType.REJECTED.value,
        "remarks": "Claim rejected based on confirmed policy exclusion matching incident.",
        "reason_codes": ["EXCLUDED"],
    }
    resp = client.post(f"/api/claims/{claim.id}/decisions", json=payload_reject, headers=headers)
    assert resp.status_code == 201, resp.text
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.REJECTED

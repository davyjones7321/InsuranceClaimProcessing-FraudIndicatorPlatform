"""Integration tests for Assessment lifecycle, engine cap enforcement, and separation of duties."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Claim, Coverage, Customer, Investigation, Policy, User
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


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def assessment_setup(db_session: Session) -> dict[str, Any]:
    """Setup a test customer, policy, coverage, claim in READY_FOR_ASSESSMENT, and roles."""
    customer = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-ASSESS-001",
        full_name="Bruce Banner",
        dob=date(1980, 5, 10),
        email="bruce.assess@example.com",
        phone="+15551122",
        address="100 Gamma Lab Way",
        state_region="CA",
    )
    user_cust = User(
        id=uuid.uuid4(),
        email="bruce.assess@example.com",
        password_hash=hash_password("BrucePass1234!"),
        role=UserRole.CUSTOMER,
        customer_id=customer.id,
        is_active=True,
    )
    user_staff = User(
        id=uuid.uuid4(),
        email="staff.assess@example.com",
        password_hash=hash_password("StaffPass1234!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_assessor = User(
        id=uuid.uuid4(),
        email="assessor.assess@example.com",
        password_hash=hash_password("AssessorPass1234!"),
        role=UserRole.ASSESSOR,
        is_active=True,
    )
    user_investigator = User(
        id=uuid.uuid4(),
        email="investigator.assess@example.com",
        password_hash=hash_password("InvestigatorPass1234!"),
        role=UserRole.INVESTIGATOR,
        is_active=True,
    )
    user_manager = User(
        id=uuid.uuid4(),
        email="manager.assess@example.com",
        password_hash=hash_password("ManagerPass1234!"),
        role=UserRole.MANAGER,
        is_active=True,
    )

    db_session.add_all(
        [customer, user_cust, user_staff, user_assessor, user_investigator, user_manager]
    )
    db_session.flush()

    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-ASSESS-001",
        customer_id=customer.id,
        product_type=ProductType.HEALTH,
        start_date=date.today() - timedelta(days=60),
        end_date=date.today() + timedelta(days=300),
        premium=Decimal("25000.00"),
        sum_insured=Decimal("500000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(policy)
    db_session.flush()

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="HOSPITALIZATION",
        limit_amount=Decimal("200000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("1000.00"),
        copay_percent=Decimal("10.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)
    db_session.flush()

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-000301",
        policy_id=policy.id,
        customer_id=customer.id,
        claim_category="HOSPITALIZATION",
        incident_date=date.today() - timedelta(days=10),
        incident_location="Gamma Hospital",
        incident_description="Medical treatment expenses.",
        requested_amount=Decimal("50000.00"),
        registered_by=user_staff.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.READY_FOR_ASSESSMENT,
        status_version=3,
    )
    db_session.add(claim)
    db_session.commit()

    return {
        "customer": customer,
        "user_cust": user_cust,
        "user_staff": user_staff,
        "user_assessor": user_assessor,
        "user_investigator": user_investigator,
        "user_manager": user_manager,
        "policy": policy,
        "coverage": coverage,
        "claim": claim,
    }


def test_successful_assessment_and_state_transition(
    client: TestClient, db_session: Session, assessment_setup: dict[str, Any]
) -> None:
    """Assessor submits an assessment within the engine cap; claim advances to ASSESSED."""
    claim = assessment_setup["claim"]
    assessor = assessment_setup["user_assessor"]
    headers = auth_headers(db_session, assessor)

    # Health: loss = 10,000, deductible = 1,000 -> net = 9,000; copay 10% -> 8,100
    payload = {
        "assessed_loss": "10000.00",
        "eligible_amount": "8100.00",
        "remarks": "Invoices validated against hospital tariff sheet.",
        "supporting_info": "Room rent and pharmacy charges verified.",
    }

    resp = client.post(f"/api/claims/{claim.id}/assessments", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["claim_id"] == str(claim.id)
    assert data["assessor_id"] == str(assessor.id)
    assert data["max_eligible_amount"] == "8100.00"
    assert data["eligible_amount"] == "8100.00"

    # Database check: claim advanced to ASSESSED
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.ASSESSED
    assert claim.status_version == 4


def test_assessment_eligible_amount_above_cap_blocked(
    client: TestClient, db_session: Session, assessment_setup: dict[str, Any]
) -> None:
    """Submitting an eligible amount greater than max_eligible_amount must be rejected."""
    claim = assessment_setup["claim"]
    assessor = assessment_setup["user_assessor"]
    headers = auth_headers(db_session, assessor)

    # Cap is 8,100.00; assessor tries 9,000.00
    payload = {
        "assessed_loss": "10000.00",
        "eligible_amount": "9000.00",
        "remarks": "Assessor attempting to override engine cap.",
    }

    resp = client.post(f"/api/claims/{claim.id}/assessments", json=payload, headers=headers)
    assert resp.status_code == 400
    assert "exceeds maximum eligible cap" in resp.json()["detail"]


def test_assessment_blocked_if_open_investigation(
    client: TestClient, db_session: Session, assessment_setup: dict[str, Any]
) -> None:
    """Claim cannot be assessed while an active investigation is open."""
    claim = assessment_setup["claim"]
    assessor = assessment_setup["user_assessor"]
    headers = auth_headers(db_session, assessor)

    # Create open investigation
    inv = Investigation(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assigned_by=assessment_setup["user_manager"].id,
        status=InvestigationStatus.UNDER_REVIEW,
    )
    db_session.add(inv)
    db_session.commit()

    payload = {
        "assessed_loss": "10000.00",
        "eligible_amount": "8100.00",
        "remarks": "Assessing while investigation is open.",
    }

    resp = client.post(f"/api/claims/{claim.id}/assessments", json=payload, headers=headers)
    assert resp.status_code == 400
    assert "active investigation is open" in resp.json()["detail"]


def test_assessment_separation_of_duties_claimant_blocked(
    client: TestClient, db_session: Session, assessment_setup: dict[str, Any]
) -> None:
    """User who registered the claim cannot assess it."""
    claim = assessment_setup["claim"]
    # staff registered the claim
    staff = assessment_setup["user_staff"]
    # Temporarily grant assessor role to test separation of duties specifically
    staff.role = UserRole.ASSESSOR
    db_session.commit()
    headers = auth_headers(db_session, staff)

    payload = {
        "assessed_loss": "10000.00",
        "eligible_amount": "8100.00",
        "remarks": "Claimant trying to assess own registered claim.",
    }

    resp = client.post(f"/api/claims/{claim.id}/assessments", json=payload, headers=headers)
    assert resp.status_code == 403
    assert "Separation of duties violation" in resp.json()["detail"]


def test_assessment_separation_of_duties_investigator_blocked(
    client: TestClient, db_session: Session, assessment_setup: dict[str, Any]
) -> None:
    """Investigator assigned to this claim cannot assess it."""
    claim = assessment_setup["claim"]
    investigator = assessment_setup["user_investigator"]

    # Assign investigator to completed investigation on this claim
    inv = Investigation(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assigned_to=investigator.id,
        assigned_by=assessment_setup["user_manager"].id,
        status=InvestigationStatus.COMPLETED,
    )
    db_session.add(inv)
    db_session.commit()

    # If investigator has assessor capabilities, still blocked by SOD
    investigator.role = UserRole.ASSESSOR
    db_session.commit()
    headers = auth_headers(db_session, investigator)

    payload = {
        "assessed_loss": "10000.00",
        "eligible_amount": "8100.00",
        "remarks": "Assigned investigator trying to assess.",
    }

    resp = client.post(f"/api/claims/{claim.id}/assessments", json=payload, headers=headers)
    assert resp.status_code == 403
    assert "Separation of duties violation" in resp.json()["detail"]

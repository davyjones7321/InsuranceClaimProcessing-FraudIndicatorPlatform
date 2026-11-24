"""Integration tests for Investigation workflow, findings, escalation, and completion routing."""

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
    InvestigationOutcome,
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
def investigation_setup(db_session: Session) -> dict[str, Any]:
    """Setup claim in UNDER_INVESTIGATION with an open ASSIGNED investigation record."""
    customer = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-INV-001",
        full_name="Tony Stark",
        dob=date(1975, 4, 1),
        email="tony.inv@example.com",
        phone="+15559988",
        address="10880 Malibu Point",
        state_region="CA",
    )
    user_cust = User(
        id=uuid.uuid4(),
        email="tony.inv@example.com",
        password_hash=hash_password("TonyPassSecure123!"),
        role=UserRole.CUSTOMER,
        customer_id=customer.id,
        is_active=True,
    )
    user_staff = User(
        id=uuid.uuid4(),
        email="staff.inv@example.com",
        password_hash=hash_password("StaffPassSecure123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_investigator = User(
        id=uuid.uuid4(),
        email="sherlock.inv@example.com",
        password_hash=hash_password("SherlockPass123!"),
        role=UserRole.INVESTIGATOR,
        is_active=True,
    )
    user_manager = User(
        id=uuid.uuid4(),
        email="fury.inv@example.com",
        password_hash=hash_password("FuryPassSecure123!"),
        role=UserRole.MANAGER,
        is_active=True,
    )

    db_session.add_all([customer, user_cust, user_staff, user_investigator, user_manager])
    db_session.flush()

    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-INV-001",
        customer_id=customer.id,
        product_type=ProductType.PROPERTY,
        start_date=date.today() - timedelta(days=90),
        end_date=date.today() + timedelta(days=275),
        premium=Decimal("45000.00"),
        sum_insured=Decimal("1500000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(policy)
    db_session.flush()

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="FIRE",
        limit_amount=Decimal("1000000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("10000.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)
    db_session.flush()

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-000401",
        policy_id=policy.id,
        customer_id=customer.id,
        claim_category="FIRE",
        incident_date=date.today() - timedelta(days=15),
        incident_location="Malibu Workshop",
        incident_description="Workshop electrical fire.",
        requested_amount=Decimal("250000.00"),
        registered_by=user_staff.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.UNDER_INVESTIGATION,
        status_version=2,
    )
    db_session.add(claim)
    db_session.flush()

    investigation = Investigation(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assigned_to=None,
        assigned_by=user_manager.id,
        status=InvestigationStatus.ASSIGNED,
    )
    db_session.add(investigation)
    db_session.commit()

    return {
        "customer": customer,
        "user_cust": user_cust,
        "user_staff": user_staff,
        "user_investigator": user_investigator,
        "user_manager": user_manager,
        "claim": claim,
        "investigation": investigation,
    }


def test_investigator_assignment(
    client: TestClient, db_session: Session, investigation_setup: dict[str, Any]
) -> None:
    """Manager can assign an investigator; staff cannot."""
    claim = investigation_setup["claim"]
    investigator = investigation_setup["user_investigator"]
    manager = investigation_setup["user_manager"]
    staff = investigation_setup["user_staff"]

    # Staff attempt should fail with 403
    staff_headers = auth_headers(db_session, staff)
    resp = client.post(
        f"/api/claims/{claim.id}/investigations/assign",
        json={"investigator_id": str(investigator.id)},
        headers=staff_headers,
    )
    assert resp.status_code == 403

    # Manager attempt succeeds
    mgr_headers = auth_headers(db_session, manager)
    resp = client.post(
        f"/api/claims/{claim.id}/investigations/assign",
        json={"investigator_id": str(investigator.id)},
        headers=mgr_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["assigned_to"] == str(investigator.id)


def test_findings_and_completion_lifecycle(
    client: TestClient, db_session: Session, investigation_setup: dict[str, Any]
) -> None:
    """Investigator records findings and completes investigation; claim routes to READY_FOR_ASSESSMENT."""
    claim = investigation_setup["claim"]
    investigator = investigation_setup["user_investigator"]
    investigation = investigation_setup["investigation"]

    # Assign investigator
    investigation.assigned_to = investigator.id
    db_session.commit()

    inv_headers = auth_headers(db_session, investigator)

    # 1. Attempting to complete with zero findings fails
    resp = client.post(
        f"/api/claims/{claim.id}/investigations/complete",
        json={"outcome": InvestigationOutcome.CLEARED.value, "remarks": "Looks fine"},
        headers=inv_headers,
    )
    assert resp.status_code == 400
    assert "requires at least one finding" in resp.json()["detail"]

    # 2. Add finding
    resp = client.post(
        f"/api/claims/{claim.id}/investigations/findings",
        json={"finding_text": "Inspected scene of fire: cause confirmed to be faulty transformer."},
        headers=inv_headers,
    )
    assert resp.status_code == 201, resp.text
    finding_data = resp.json()
    assert finding_data["investigation_id"] == str(investigation.id)

    # 3. Complete investigation
    resp = client.post(
        f"/api/claims/{claim.id}/investigations/complete",
        json={
            "outcome": InvestigationOutcome.CLEARED.value,
            "remarks": "Claim cleared after on-site audit.",
        },
        headers=inv_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == InvestigationStatus.COMPLETED.value
    assert resp.json()["outcome"] == InvestigationOutcome.CLEARED.value

    # 4. Verify claim transitioned to READY_FOR_ASSESSMENT
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.READY_FOR_ASSESSMENT
    assert (
        claim.status_version == 4
    )  # incremented twice (UNDER_INVESTIGATION -> INVESTIGATION_COMPLETE -> READY_FOR_ASSESSMENT)


def test_investigation_escalation_and_manager_resolution(
    client: TestClient, db_session: Session, investigation_setup: dict[str, Any]
) -> None:
    """Investigator escalates investigation; only a manager can resolve/complete it."""
    claim = investigation_setup["claim"]
    investigator = investigation_setup["user_investigator"]
    manager = investigation_setup["user_manager"]
    investigation = investigation_setup["investigation"]

    investigation.assigned_to = investigator.id
    db_session.commit()

    inv_headers = auth_headers(db_session, investigator)

    # Add initial finding
    client.post(
        f"/api/claims/{claim.id}/investigations/findings",
        json={"finding_text": "Evidence of accelerated arson detected at commercial site."},
        headers=inv_headers,
    )

    # Escalate
    resp = client.post(
        f"/api/claims/{claim.id}/investigations/escalate",
        json={"reason": "Requires forensic legal review due to suspected arson."},
        headers=inv_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == InvestigationStatus.ESCALATED.value

    # Database: claim status must be ESCALATED
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.ESCALATED

    # Investigator attempts to complete -> 403 Forbidden
    resp = client.post(
        f"/api/claims/{claim.id}/investigations/complete",
        json={"outcome": InvestigationOutcome.CONCERNS_CONFIRMED.value},
        headers=inv_headers,
    )
    assert resp.status_code == 403
    assert "Only a manager or admin can resolve an escalated investigation" in resp.json()["detail"]

    # Manager completes
    mgr_headers = auth_headers(db_session, manager)
    resp = client.post(
        f"/api/claims/{claim.id}/investigations/complete",
        json={
            "outcome": InvestigationOutcome.CONCERNS_CONFIRMED.value,
            "remarks": "Manager reviewed forensics: concerns confirmed.",
        },
        headers=mgr_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == InvestigationStatus.COMPLETED.value

    # Claim status routed to READY_FOR_ASSESSMENT
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.READY_FOR_ASSESSMENT

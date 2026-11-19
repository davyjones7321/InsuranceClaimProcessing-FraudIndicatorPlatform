"""Integration tests for State Machine pipeline, optimistic locking, and transition auditing."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Claim, ClaimStatusHistory, Customer, Policy, User
from app.domain.enums import ClaimChannel, ClaimStatus, PolicyStatus, ProductType, UserRole
from app.security.password import hash_password
from app.security.session import create_session


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def pipeline_setup(db_session: Session) -> dict[str, Any]:
    """Setup a registered claim and users for state machine tests."""
    cust = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-PIPE-001",
        full_name="Peter Parker",
        dob=date(1995, 8, 10),
        email="peter.pipe@example.com",
        phone="+15554321",
        address="20 Queens Blvd",
        state_region="NY",
    )
    user_cust = User(
        id=uuid.uuid4(),
        email="peter.pipe@example.com",
        password_hash=hash_password("PeterPassSecure123!"),
        role=UserRole.CUSTOMER,
        customer_id=cust.id,
        is_active=True,
    )
    user_staff = User(
        id=uuid.uuid4(),
        email="staff.pipe@example.com",
        password_hash=hash_password("StaffPipePass123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_manager = User(
        id=uuid.uuid4(),
        email="manager.pipe@example.com",
        password_hash=hash_password("ManagerPipePass123!"),
        role=UserRole.MANAGER,
        is_active=True,
    )

    db_session.add_all([cust, user_cust, user_staff, user_manager])
    db_session.flush()

    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-PIPE-001",
        customer_id=cust.id,
        product_type=ProductType.PROPERTY,
        start_date=date.today() - timedelta(days=50),
        end_date=date.today() + timedelta(days=315),
        premium=Decimal("35000.00"),
        sum_insured=Decimal("1200000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(policy)
    db_session.flush()

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-000201",
        policy_id=policy.id,
        customer_id=cust.id,
        claim_category="FIRE",
        incident_date=date.today() - timedelta(days=10),
        incident_location="Queens, NY",
        incident_description="Kitchen electrical fire.",
        requested_amount=Decimal("150000.00"),
        registered_by=user_staff.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.REGISTERED,
        status_version=1,
    )
    db_session.add(claim)
    db_session.commit()

    return {
        "cust": cust,
        "user_cust": user_cust,
        "user_staff": user_staff,
        "user_manager": user_manager,
        "policy": policy,
        "claim": claim,
    }


def test_state_machine_transition_and_optimistic_locking(
    client: TestClient, db_session: Session, pipeline_setup: dict[str, Any]
) -> None:
    """Legal transition advances state, increments status_version, and rejects stale versions with 409 Conflict."""
    claim = pipeline_setup["claim"]
    user_staff = pipeline_setup["user_staff"]
    hdr = auth_headers(db_session, user_staff)

    # 1. Transition REGISTERED -> DOCUMENTS_PENDING with expected_status_version=1
    res1 = client.patch(
        f"/api/claims/{claim.id}/status",
        headers=hdr,
        json={
            "to_status": "DOCUMENTS_PENDING",
            "expected_status_version": 1,
            "remarks": "Missing mandatory fire department incident report.",
        },
    )
    assert res1.status_code == 200
    updated = res1.json()
    assert updated["status"] == "DOCUMENTS_PENDING"
    assert updated["status_version"] == 2

    # 2. Re-submitting with stale version (expected_status_version=1) must return 409 Conflict
    res_stale = client.patch(
        f"/api/claims/{claim.id}/status",
        headers=hdr,
        json={
            "to_status": "REGISTERED",
            "expected_status_version": 1,  # Stale! Current is 2
            "remarks": "Documents provided",
        },
    )
    assert res_stale.status_code == 409
    assert "Stale claim state" in res_stale.json()["detail"]


def test_illegal_state_transition_rejected(
    client: TestClient, db_session: Session, pipeline_setup: dict[str, Any]
) -> None:
    """Illegal transitions (e.g. REGISTERED directly to APPROVED) must fail with 422 Unprocessable Entity."""
    claim = pipeline_setup["claim"]
    user_manager = pipeline_setup["user_manager"]
    hdr = auth_headers(db_session, user_manager)

    res = client.patch(
        f"/api/claims/{claim.id}/status",
        headers=hdr,
        json={
            "to_status": "APPROVED",
            "expected_status_version": 1,
            "remarks": "Attempting illegal direct approval",
        },
    )
    assert res.status_code == 422
    assert "Illegal claim status transition" in res.json()["detail"]


def test_documents_pending_to_rejected_requires_remarks_and_reason_code(
    client: TestClient, db_session: Session, pipeline_setup: dict[str, Any]
) -> None:
    """Section G.77: Rejecting from DOCUMENTS_PENDING requires mandatory remarks and reason_code."""
    claim = pipeline_setup["claim"]
    user_manager = pipeline_setup["user_manager"]
    hdr = auth_headers(db_session, user_manager)

    # Move to DOCUMENTS_PENDING first
    client.patch(
        f"/api/claims/{claim.id}/status",
        headers=hdr,
        json={
            "to_status": "DOCUMENTS_PENDING",
            "expected_status_version": 1,
            "remarks": "Documents required",
        },
    )

    # 1. Attempting rejection without reason_code fails with 400
    res_no_code = client.patch(
        f"/api/claims/{claim.id}/status",
        headers=hdr,
        json={
            "to_status": "REJECTED",
            "expected_status_version": 2,
            "remarks": "Failed to supply requested proof of loss.",
        },
    )
    assert res_no_code.status_code == 400
    assert "reason_code is mandatory" in res_no_code.json()["detail"]

    # 2. Rejection with reason_code and remarks succeeds
    res_rejected = client.patch(
        f"/api/claims/{claim.id}/status",
        headers=hdr,
        json={
            "to_status": "REJECTED",
            "expected_status_version": 2,
            "reason_code": "DOCUMENTS_NOT_PROVIDED",
            "remarks": "Required proof of loss was never provided within 30 days.",
        },
    )
    assert res_rejected.status_code == 200
    assert res_rejected.json()["status"] == "REJECTED"
    assert res_rejected.json()["status_version"] == 3

    # Verify history recorded
    history = (
        db_session.query(ClaimStatusHistory)
        .filter_by(claim_id=claim.id, to_status="REJECTED")
        .first()
    )
    assert history is not None
    assert "Required proof of loss was never provided" in history.reason

    # Verify audit event written
    audit = (
        db_session.query(AuditLog)
        .filter_by(chain_key=f"claim:{claim.id}", action="CLAIM_STATUS_TRANSITION")
        .order_by(AuditLog.seq.desc())
        .first()
    )
    assert audit is not None
    assert audit.event_data["to_status"] == "REJECTED"
    assert audit.event_data["reason_code"] == "DOCUMENTS_NOT_PROVIDED"

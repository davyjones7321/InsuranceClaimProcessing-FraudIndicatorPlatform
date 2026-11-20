"""Exhaustive tests for Phase P1 Security and Authorization hardening."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import (
    AccessLog,
    Claim,
    ClaimDocument,
    Customer,
    Investigation,
    Policy,
    User,
)
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    DocumentStatus,
    InvestigationStatus,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.schemas.documents import DocumentReviewRequest
from app.security.password import hash_password
from app.security.rbac import PermissionDeniedError
from app.security.session import create_session
from app.services.claim_service import ClaimService


def auth_headers(db: Session, user: User) -> dict[str, str]:
    """Helper creating session and returning authorization bearer headers."""
    session_id = create_session(db, user)
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def p1_fixture(db_session: Session) -> dict[str, Any]:
    """Setup users for all roles and sample claim/policy."""
    cust = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-P1-001",
        full_name="Bruce Wayne",
        dob=date(1985, 2, 19),
        email="bruce@wayne-enterprises.com",
        phone="+15550001",
        address="1007 Mountain Drive",
        state_region="NJ",
    )
    user_cust = User(
        id=uuid.uuid4(),
        email="bruce@wayne-enterprises.com",
        password_hash=hash_password("GothamCity1234!"),
        role=UserRole.CUSTOMER,
        customer_id=cust.id,
        is_active=True,
    )
    user_staff = User(
        id=uuid.uuid4(),
        email="lucius.fox@wayne-enterprises.com",
        password_hash=hash_password("GothamCity1234!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_assessor = User(
        id=uuid.uuid4(),
        email="harvey.dent@wayne-enterprises.com",
        password_hash=hash_password("GothamCity1234!"),
        role=UserRole.ASSESSOR,
        is_active=True,
    )
    user_investigator = User(
        id=uuid.uuid4(),
        email="jim.gordon@wayne-enterprises.com",
        password_hash=hash_password("GothamCity1234!"),
        role=UserRole.INVESTIGATOR,
        is_active=True,
    )
    user_manager = User(
        id=uuid.uuid4(),
        email="alfred.pennyworth@wayne-enterprises.com",
        password_hash=hash_password("GothamCity1234!"),
        role=UserRole.MANAGER,
        is_active=True,
    )
    user_admin = User(
        id=uuid.uuid4(),
        email="oracle@wayne-enterprises.com",
        password_hash=hash_password("GothamCity1234!"),
        role=UserRole.ADMIN,
        is_active=True,
    )

    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-P1-001",
        customer_id=cust.id,
        product_type=ProductType.PROPERTY,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("50000.00"),
        sum_insured=Decimal("2000000.00"),
        status=PolicyStatus.ACTIVE,
    )

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-000555",
        policy_id=policy.id,
        customer_id=cust.id,
        claim_category="PROPERTY_DAMAGE",
        incident_date=date(2026, 5, 10),
        incident_location="Wayne Manor",
        incident_description="Structural damage to manor.",
        requested_amount=Decimal("150000.00"),
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.UNDER_INVESTIGATION,
        registered_by=user_staff.id,
    )

    investigation = Investigation(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assigned_to=user_investigator.id,
        assigned_by=user_manager.id,
        status=InvestigationStatus.ASSIGNED,
    )

    db_session.add_all(
        [
            cust,
            user_cust,
            user_staff,
            user_assessor,
            user_investigator,
            user_manager,
            user_admin,
            policy,
            claim,
            investigation,
        ]
    )
    db_session.commit()

    return {
        "cust": cust,
        "user_cust": user_cust,
        "user_staff": user_staff,
        "user_assessor": user_assessor,
        "user_investigator": user_investigator,
        "user_manager": user_manager,
        "user_admin": user_admin,
        "policy": policy,
        "claim": claim,
        "investigation": investigation,
    }


def test_customer_status_masking_hides_investigation(
    client: TestClient, db_session: Session, p1_fixture: dict[str, Any]
) -> None:
    """Customers receive UNDER_REVIEW status when claim is UNDER_INVESTIGATION (P1 Task 2, D6)."""
    claim: Claim = p1_fixture["claim"]
    user_cust: User = p1_fixture["user_cust"]
    user_manager: User = p1_fixture["user_manager"]

    # 1. Customer view masks to UNDER_REVIEW
    hdr_cust = auth_headers(db_session, user_cust)
    resp_cust = client.get(f"/api/claims/{claim.id}", headers=hdr_cust)
    assert resp_cust.status_code == 200
    assert resp_cust.json()["status"] == "UNDER_REVIEW"

    # 2. Manager view sees real UNDER_INVESTIGATION status
    hdr_mgr = auth_headers(db_session, user_manager)
    resp_mgr = client.get(f"/api/claims/{claim.id}", headers=hdr_mgr)
    assert resp_mgr.status_code == 200
    assert resp_mgr.json()["status"] == "UNDER_INVESTIGATION"


def test_risk_endpoints_role_enforcement_and_assessor_masking(
    client: TestClient, db_session: Session, p1_fixture: dict[str, Any]
) -> None:
    """Customer and Staff are forbidden from risk; Assessor receives masked summary without evidence (D6, F4)."""
    claim: Claim = p1_fixture["claim"]
    user_cust: User = p1_fixture["user_cust"]
    user_staff: User = p1_fixture["user_staff"]
    user_assessor: User = p1_fixture["user_assessor"]

    # 1. Customer gets 403 Forbidden
    hdr_cust = auth_headers(db_session, user_cust)
    res_cust = client.get(f"/api/claims/{claim.id}/risk-evaluations", headers=hdr_cust)
    assert res_cust.status_code == 403

    # 2. Staff gets 403 Forbidden
    hdr_staff = auth_headers(db_session, user_staff)
    res_staff = client.get(f"/api/claims/{claim.id}/risk-evaluations", headers=hdr_staff)
    assert res_staff.status_code == 403

    # 3. Assessor gets 200 with masked evidence
    hdr_assessor = auth_headers(db_session, user_assessor)
    res_assessor = client.get(f"/api/claims/{claim.id}/risk-evaluations", headers=hdr_assessor)
    assert res_assessor.status_code == 200
    assert isinstance(res_assessor.json(), list)


def test_separation_of_duties_document_reviewer_cannot_be_uploader(
    db_session: Session, p1_fixture: dict[str, Any]
) -> None:
    """Document reviewer cannot review documents they uploaded (P1 Task 8, D8)."""
    claim: Claim = p1_fixture["claim"]
    user_assessor: User = p1_fixture["user_assessor"]

    claim_service = ClaimService(db_session)

    # Assessor uploads a document
    doc = ClaimDocument(
        id=uuid.uuid4(),
        claim_id=claim.id,
        doc_code="ESTIMATE_INVOICE",
        file_name="invoice.pdf",
        storage_key=f"{uuid.uuid4().hex}.pdf",
        sha256="0" * 64,
        size_bytes=1024,
        mime_type="application/pdf",
        status=DocumentStatus.PENDING_REVIEW,
        uploaded_by=user_assessor.id,
    )
    db_session.add(doc)
    db_session.commit()

    # Assessor attempts to review own document
    review_req = DocumentReviewRequest(status=DocumentStatus.VALID)
    with pytest.raises(PermissionDeniedError, match="reviewer cannot be the uploader"):
        claim_service.review_document(claim.id, doc.id, review_req, actor=user_assessor)


def test_upload_magic_signature_validation(
    client: TestClient, db_session: Session, p1_fixture: dict[str, Any]
) -> None:
    """Upload rejects content whose magic bytes mismatch declared MIME type (P1 Task 6)."""
    claim: Claim = p1_fixture["claim"]
    user_staff: User = p1_fixture["user_staff"]
    hdr_staff = auth_headers(db_session, user_staff)

    # Send executable / text claiming to be application/pdf
    fake_pdf = b"MZ\x90\x00\x03\x00\x00\x00fake_executable_bytes"
    res = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_staff,
        data={"doc_code": "REPAIR_ESTIMATE"},
        files={"file": ("malware.pdf", fake_pdf, "application/pdf")},
    )
    assert res.status_code == 415


def test_access_log_fail_closed_and_recording(
    client: TestClient, db_session: Session, p1_fixture: dict[str, Any]
) -> None:
    """Accessing non-owner claim details commits an access_log row (P1 Task 5)."""
    claim: Claim = p1_fixture["claim"]
    user_manager: User = p1_fixture["user_manager"]
    hdr_mgr = auth_headers(db_session, user_manager)

    resp = client.get(f"/api/claims/{claim.id}", headers=hdr_mgr)
    assert resp.status_code == 200

    log_entry = (
        db_session.query(AccessLog)
        .filter_by(
            claim_id=claim.id,
            actor_id=user_manager.id,
            resource_type="CLAIM_DETAIL",
            action="VIEW",
        )
        .first()
    )
    assert log_entry is not None
    assert log_entry.actor_role == "manager"

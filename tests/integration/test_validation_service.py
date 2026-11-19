"""Integration tests for ValidationService, REST endpoints, and determinism replay."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.audit.verifier import verify_chain
from app.db.models import (
    Claim,
    Coverage,
    Customer,
    Policy,
    RequiredDocumentType,
    User,
)
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    DocumentStatus,
    PolicyStatus,
    ProductType,
    UserRole,
    ValidationOutcome,
)
from app.security.password import hash_password
from app.security.session import create_session
from app.services.claim_service import ClaimService
from app.services.validation_service import ValidationService
from scripts.replay import replay_validation_run


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def validation_fixture(db_session: Session) -> dict[str, Any]:
    """Setup users, customer, policy, coverage, required docs, and registered claim."""
    cust = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-VAL-001",
        full_name="Bob Builder",
        dob=date(1985, 5, 20),
        email="bob.builder@example.com",
        phone="+15559999",
        address="77 Wall Street",
        state_region="NY",
    )
    user_cust = User(
        id=uuid.uuid4(),
        email="bob.cust@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.CUSTOMER,
        customer_id=cust.id,
        is_active=True,
    )
    user_other_cust = User(
        id=uuid.uuid4(),
        email="other.cust@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.CUSTOMER,
        customer_id=uuid.uuid4(),
        is_active=True,
    )
    user_staff = User(
        id=uuid.uuid4(),
        email="staff.val@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_assessor = User(
        id=uuid.uuid4(),
        email="assessor.val@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.ASSESSOR,
        is_active=True,
    )
    user_admin = User(
        id=uuid.uuid4(),
        email="admin.val@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.ADMIN,
        is_active=True,
    )

    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-VAL-HEALTH-001",
        customer_id=cust.id,
        product_type=ProductType.HEALTH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("1200.00"),
        status=PolicyStatus.ACTIVE,
        sum_insured=Decimal("100000.00"),
    )

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="CAT_SURGERY",
        limit_amount=Decimal("50000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("1000.00"),
        copay_percent=Decimal("10.00"),
        waiting_period_days=30,
        is_excluded=False,
    )

    req_doc = RequiredDocumentType(
        id=uuid.uuid4(),
        product_type=ProductType.HEALTH,
        claim_category="CAT_SURGERY",
        doc_code="DOC_MED_BILL",
        name="Hospital Medical Bill",
        is_mandatory=True,
    )

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-900001",
        policy_id=policy.id,
        customer_id=cust.id,
        claim_category="CAT_SURGERY",
        incident_date=date(2026, 3, 1),
        incident_location="Metropolitan Hospital, NY",
        incident_description="Emergency appendectomy surgery",
        requested_amount=Decimal("4500.00"),
        registered_at=datetime(2026, 3, 5, 10, 0, tzinfo=UTC),
        registered_by=user_staff.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.REGISTERED,
        status_version=1,
    )

    db_session.add_all([cust, user_cust, user_other_cust, user_staff, user_assessor, user_admin, policy, coverage, req_doc, claim])
    db_session.commit()

    return {
        "cust": cust,
        "user_cust": user_cust,
        "user_other_cust": user_other_cust,
        "user_staff": user_staff,
        "user_assessor": user_assessor,
        "user_admin": user_admin,
        "policy": policy,
        "coverage": coverage,
        "req_doc": req_doc,
        "claim": claim,
    }


def test_validation_missing_documents_blocks_claim(
    client: TestClient, db_session: Session, validation_fixture: dict[str, Any]
) -> None:
    claim: Claim = validation_fixture["claim"]
    admin: User = validation_fixture["user_admin"]
    headers = auth_headers(db_session, admin)

    # 1. Validate claim without documents uploaded -> Should yield BLOCKED_DOCS and DOCUMENTS_PENDING
    resp = client.post(f"/api/claims/{claim.id}/validate", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["overall_result"] == "BLOCKED_DOCS"
    assert data["claim_id"] == str(claim.id)
    assert any(r["rule_id"] == "V13" and r["outcome"] == "FAIL" for r in data["results"])

    db_session.refresh(claim)
    assert claim.status == ClaimStatus.DOCUMENTS_PENDING
    assert claim.status_version == 2

    # Check status history was written
    assert len(claim.status_history) == 1
    assert claim.status_history[0].from_status == "REGISTERED"
    assert claim.status_history[0].to_status == "DOCUMENTS_PENDING"


def test_document_upload_and_review_unblocks_claim_validation(
    client: TestClient, db_session: Session, validation_fixture: dict[str, Any]
) -> None:
    claim: Claim = validation_fixture["claim"]
    admin: User = validation_fixture["user_admin"]
    staff: User = validation_fixture["user_staff"]
    assessor: User = validation_fixture["user_assessor"]

    claim_service = ClaimService(db_session)
    val_service = ValidationService(db_session)

    # Step 1: Initial validation blocks on missing document
    val_run1 = val_service.validate_claim(claim.id, admin, evaluation_date=date(2026, 3, 10))
    assert val_run1.overall_result == ValidationOutcome.BLOCKED_DOCS
    assert claim.status == ClaimStatus.DOCUMENTS_PENDING

    # Step 2: Upload mandatory document (DOC_MED_BILL)
    doc_binary = b"%PDF-1.4 Mock valid medical bill document content"
    uploaded_doc = claim_service.upload_document(
        claim_id=claim.id,
        doc_code="DOC_MED_BILL",
        file_name="medical_bill.pdf",
        content=doc_binary,
        mime_type="application/pdf",
        actor=staff,
    )
    assert uploaded_doc.status == DocumentStatus.PENDING_REVIEW

    # Step 3: Review document as VALID
    from app.schemas.documents import DocumentReviewRequest
    reviewed_doc = claim_service.review_document(
        claim_id=claim.id,
        document_id=uploaded_doc.id,
        review_req=DocumentReviewRequest(status=DocumentStatus.VALID, review_remarks="Verified by billing department"),
        actor=assessor,
    )
    assert reviewed_doc.status == DocumentStatus.VALID
    db_session.commit()

    # Step 4: Re-run validation -> should transition DOCUMENTS_PENDING -> REGISTERED -> VALIDATED
    val_run2 = val_service.validate_claim(claim.id, staff, evaluation_date=date(2026, 3, 10))
    assert val_run2.overall_result == ValidationOutcome.PASS

    db_session.refresh(claim)
    assert claim.status == ClaimStatus.VALIDATED

    # Verify history chain
    statuses = [(h.from_status, h.to_status) for h in claim.status_history]
    assert ("REGISTERED", "DOCUMENTS_PENDING") in statuses
    assert ("DOCUMENTS_PENDING", "REGISTERED") in statuses
    assert ("REGISTERED", "VALIDATED") in statuses

    # Verify audit chain integrity
    audit_res = verify_chain(db_session, chain_key=f"claim:{claim.id}", record_run=False)
    assert audit_res.is_valid, f"Audit chain broken: {audit_res.error_message}"
    assert audit_res.total_events >= 5


def test_validation_failure_on_business_rule_violation(
    client: TestClient, db_session: Session, validation_fixture: dict[str, Any]
) -> None:
    claim: Claim = validation_fixture["claim"]
    admin: User = validation_fixture["user_admin"]

    # Alter incident date to before policy start date (outside policy period)
    claim.incident_date = date(2025, 12, 1)  # Policy starts 2026-01-01
    db_session.commit()

    headers = auth_headers(db_session, admin)
    resp = client.post(f"/api/claims/{claim.id}/validate", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["overall_result"] == "FAIL"
    v03_res = next(r for r in data["results"] if r["rule_id"] == "V03")
    assert v03_res["outcome"] == "FAIL"
    assert v03_res["reason_code"] == "INCIDENT_OUTSIDE_POLICY_PERIOD"

    db_session.refresh(claim)
    assert claim.status == ClaimStatus.VALIDATION_FAILED


def test_validation_api_endpoints_and_scoping(
    client: TestClient, db_session: Session, validation_fixture: dict[str, Any]
) -> None:
    claim: Claim = validation_fixture["claim"]
    cust: User = validation_fixture["user_cust"]
    other_cust: User = validation_fixture["user_other_cust"]
    admin: User = validation_fixture["user_admin"]

    # 1. Admin triggers validation
    admin_headers = auth_headers(db_session, admin)
    resp = client.post(f"/api/claims/{claim.id}/validate", headers=admin_headers)
    assert resp.status_code == 200

    # 2. Customer reads their own validation runs
    cust_headers = auth_headers(db_session, cust)
    resp = client.get(f"/api/claims/{claim.id}/validation-runs", headers=cust_headers)
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) >= 1

    # 3. Customer reads latest validation run
    resp_latest = client.get(f"/api/claims/{claim.id}/validation-runs/latest", headers=cust_headers)
    assert resp_latest.status_code == 200
    assert resp_latest.json()["id"] == runs[0]["id"]

    # 4. Other customer attempting to view runs is forbidden (403)
    other_headers = auth_headers(db_session, other_cust)
    resp_forbid = client.get(f"/api/claims/{claim.id}/validation-runs", headers=other_headers)
    assert resp_forbid.status_code == 403


def test_deterministic_replay_on_persisted_run(
    db_session: Session, validation_fixture: dict[str, Any]
) -> None:
    claim: Claim = validation_fixture["claim"]
    admin: User = validation_fixture["user_admin"]

    val_service = ValidationService(db_session)
    stored_run = val_service.validate_claim(claim.id, admin, evaluation_date=date(2026, 3, 10))

    # Run replay function on the stored run
    is_valid, errors = replay_validation_run(db_session, stored_run, verbose=True)
    assert is_valid, f"Replay divergence detected: {errors}"
    assert len(errors) == 0


def test_run_replay_cli_on_database(
    db_session: Session, validation_fixture: dict[str, Any]
) -> None:
    claim: Claim = validation_fixture["claim"]
    admin: User = validation_fixture["user_admin"]

    val_service = ValidationService(db_session)
    val_service.validate_claim(claim.id, admin, evaluation_date=date(2026, 3, 10))

    from scripts.replay import run_replay

    code = run_replay(db=db_session, verbose=True)
    assert code == 0

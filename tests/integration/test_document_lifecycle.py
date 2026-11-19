"""Integration tests for Document upload security, lifecycle, superseding, and review."""

import hashlib
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    AuditLog,
    Claim,
    ClaimDocument,
    Customer,
    Policy,
    User,
)
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    DocumentStatus,
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
def document_test_setup(db_session: Session) -> dict[str, Any]:
    """Setup users for all relevant roles and a registered claim."""
    cust = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-DOC-001",
        full_name="Diana Prince",
        dob=date(1988, 3, 15),
        email="diana.doc@example.com",
        phone="+15559900",
        address="10 Paradise Way",
        state_region="CA",
    )
    user_cust = User(
        id=uuid.uuid4(),
        email="diana.doc@example.com",
        password_hash=hash_password("DianaSecurePass123!"),
        role=UserRole.CUSTOMER,
        customer_id=cust.id,
        is_active=True,
    )
    user_staff = User(
        id=uuid.uuid4(),
        email="staff.doc@example.com",
        password_hash=hash_password("StaffDocPass123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_assessor = User(
        id=uuid.uuid4(),
        email="assessor.doc@example.com",
        password_hash=hash_password("AssessorDocPass123!"),
        role=UserRole.ASSESSOR,
        is_active=True,
    )
    user_manager = User(
        id=uuid.uuid4(),
        email="manager.doc@example.com",
        password_hash=hash_password("ManagerDocPass123!"),
        role=UserRole.MANAGER,
        is_active=True,
    )

    db_session.add_all([cust, user_cust, user_staff, user_assessor, user_manager])
    db_session.flush()

    # Active Policy and Claim
    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-DOC-001",
        customer_id=cust.id,
        product_type=ProductType.HEALTH,
        start_date=date.today() - timedelta(days=30),
        end_date=date.today() + timedelta(days=330),
        premium=Decimal("20000.00"),
        sum_insured=Decimal("600000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(policy)
    db_session.flush()

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-000101",
        policy_id=policy.id,
        customer_id=cust.id,
        claim_category="HOSPITALIZATION",
        incident_date=date.today() - timedelta(days=5),
        incident_location="General Hospital",
        incident_description="Emergency surgery and hospitalization.",
        requested_amount=Decimal("80000.00"),
        registered_by=user_cust.id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.REGISTERED,
        status_version=1,
    )
    db_session.add(claim)
    db_session.commit()

    return {
        "cust": cust,
        "user_cust": user_cust,
        "user_staff": user_staff,
        "user_assessor": user_assessor,
        "user_manager": user_manager,
        "policy": policy,
        "claim": claim,
    }


def test_document_upload_success_and_sha256(
    client: TestClient, db_session: Session, document_test_setup: dict[str, Any]
) -> None:
    """Document upload succeeds with valid MIME type, attaches to claim, and stores accurate SHA-256."""
    user_cust = document_test_setup["user_cust"]
    claim = document_test_setup["claim"]
    hdr = auth_headers(db_session, user_cust)

    sample_content = b"%PDF-1.4 sample hospital invoice receipt and bill bytes"
    expected_sha256 = hashlib.sha256(sample_content).hexdigest()

    files = {"file": ("invoice.pdf", sample_content, "application/pdf")}
    data = {
        "doc_code": "ESTIMATE_INVOICE",
        "document_date": str(date.today() - timedelta(days=4)),
        "named_person": "Diana Prince",
    }

    res = client.post(f"/api/claims/{claim.id}/documents", headers=hdr, data=data, files=files)
    assert res.status_code == 201
    doc_info = res.json()
    assert doc_info["doc_code"] == "ESTIMATE_INVOICE"
    assert doc_info["status"] == "PENDING_REVIEW"
    assert doc_info["sha256"] == expected_sha256
    assert doc_info["size_bytes"] == len(sample_content)
    assert doc_info["file_name"] == "invoice.pdf"

    # Verify audit event written
    audit = (
        db_session.query(AuditLog)
        .filter_by(chain_key=f"claim:{claim.id}", action="DOCUMENT_UPLOADED")
        .first()
    )
    assert audit is not None
    assert audit.event_data["sha256"] == expected_sha256


def test_document_upload_security_rejects_disallowed_mime_types(
    client: TestClient, db_session: Session, document_test_setup: dict[str, Any]
) -> None:
    """Disallowed MIME types (e.g. text/html, exe, javascript) are rejected with 415 Unsupported Media Type."""
    user_cust = document_test_setup["user_cust"]
    claim = document_test_setup["claim"]
    hdr = auth_headers(db_session, user_cust)

    bad_content = b"<script>alert('xss')</script>"
    files = {"file": ("malicious.html", bad_content, "text/html")}
    data = {"doc_code": "ESTIMATE_INVOICE"}

    res = client.post(f"/api/claims/{claim.id}/documents", headers=hdr, data=data, files=files)
    assert res.status_code == 415
    assert "MIME type 'text/html' is not allowed" in res.json()["detail"]


def test_document_upload_rejects_oversized_files(
    client: TestClient,
    db_session: Session,
    document_test_setup: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files exceeding maximum allowed size are rejected with 413 Request Entity Too Large."""
    user_cust = document_test_setup["user_cust"]
    claim = document_test_setup["claim"]
    hdr = auth_headers(db_session, user_cust)

    # Temporarily set max size to 100 bytes for test
    monkeypatch.setattr(settings, "UPLOAD_MAX_SIZE_BYTES", 100)

    oversized_content = b"%PDF-1.4 " + (b"A" * 200)
    files = {"file": ("large.pdf", oversized_content, "application/pdf")}
    data = {"doc_code": "ESTIMATE_INVOICE"}

    res = client.post(f"/api/claims/{claim.id}/documents", headers=hdr, data=data, files=files)
    assert res.status_code == 413
    assert "exceeds maximum allowed limit" in res.json()["detail"]


def test_document_superseding_lifecycle(
    client: TestClient, db_session: Session, document_test_setup: dict[str, Any]
) -> None:
    """Uploading a replacement document with the same doc_code marks the prior document SUPERSEDED (Section B.16)."""
    user_cust = document_test_setup["user_cust"]
    claim = document_test_setup["claim"]
    hdr = auth_headers(db_session, user_cust)

    # 1. First upload
    res1 = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr,
        data={"doc_code": "ID_PROOF"},
        files={"file": ("id_v1.png", b"\x89PNG\r\n\x1a\nfirst_image", "image/png")},
    )
    assert res1.status_code == 201
    doc1_id = res1.json()["id"]

    # 2. Upload replacement for same doc_code
    res2 = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr,
        data={"doc_code": "ID_PROOF"},
        files={"file": ("id_v2.png", b"\x89PNG\r\n\x1a\nsecond_image", "image/png")},
    )
    assert res2.status_code == 201
    doc2_id = res2.json()["id"]

    # Verify doc 1 status changed to SUPERSEDED, and doc 2 is PENDING_REVIEW
    doc1_db = db_session.get(ClaimDocument, uuid.UUID(doc1_id))
    doc2_db = db_session.get(ClaimDocument, uuid.UUID(doc2_id))
    assert doc1_db is not None
    assert doc2_db is not None
    assert doc1_db.status == DocumentStatus.SUPERSEDED
    assert doc2_db.status == DocumentStatus.PENDING_REVIEW


def test_document_review_by_assessor_or_staff(
    client: TestClient, db_session: Session, document_test_setup: dict[str, Any]
) -> None:
    """Assessors and Staff can review documents; marking INVALID requires reason_code and remarks (Section B.17)."""
    user_cust = document_test_setup["user_cust"]
    user_assessor = document_test_setup["user_assessor"]
    user_staff = document_test_setup["user_staff"]
    claim = document_test_setup["claim"]

    hdr_cust = auth_headers(db_session, user_cust)
    hdr_assessor = auth_headers(db_session, user_assessor)
    hdr_staff = auth_headers(db_session, user_staff)

    # Upload document
    upload_res = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_cust,
        data={"doc_code": "ESTIMATE_INVOICE"},
        files={"file": ("doc.pdf", b"%PDF-1.4 test", "application/pdf")},
    )
    doc_id = upload_res.json()["id"]

    # 1. Customer cannot review documents (403)
    res_cust_review = client.patch(
        f"/api/claims/{claim.id}/documents/{doc_id}/review",
        headers=hdr_cust,
        json={"status": "VALID"},
    )
    assert res_cust_review.status_code == 403

    # 2. Marking INVALID without reason_code fails validation
    res_invalid_no_reason = client.patch(
        f"/api/claims/{claim.id}/documents/{doc_id}/review",
        headers=hdr_assessor,
        json={"status": "INVALID", "review_remarks": "Blurry document"},
    )
    assert res_invalid_no_reason.status_code == 422

    # 3. Marking INVALID with reason_code and remarks succeeds
    res_invalid_ok = client.patch(
        f"/api/claims/{claim.id}/documents/{doc_id}/review",
        headers=hdr_staff,
        json={
            "status": "INVALID",
            "reason_code": "DOCUMENT_ILLEGIBLE",
            "review_remarks": "Text is corrupted and illegible.",
        },
    )
    assert res_invalid_ok.status_code == 200
    doc_rev = res_invalid_ok.json()
    assert doc_rev["status"] == "INVALID"
    assert doc_rev["reason_code"] == "DOCUMENT_ILLEGIBLE"
    assert doc_rev["reviewed_by"] == str(user_staff.id)

    # 4. Assessor reviews to VALID
    res_valid_ok = client.patch(
        f"/api/claims/{claim.id}/documents/{doc_id}/review",
        headers=hdr_assessor,
        json={"status": "VALID"},
    )
    assert res_valid_ok.status_code == 200
    assert res_valid_ok.json()["status"] == "VALID"


def test_sensitive_document_access_control_and_view_logging(
    client: TestClient, db_session: Session, document_test_setup: dict[str, Any]
) -> None:
    """Sensitive documents (e.g. MEDICAL_REPORT) deny access to customer/staff and log downloads (Section H.88)."""
    user_cust = document_test_setup["user_cust"]
    user_staff = document_test_setup["user_staff"]
    user_assessor = document_test_setup["user_assessor"]
    claim = document_test_setup["claim"]

    hdr_cust = auth_headers(db_session, user_cust)
    hdr_staff = auth_headers(db_session, user_staff)
    hdr_assessor = auth_headers(db_session, user_assessor)

    # Upload sensitive medical document
    upload_res = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_cust,
        data={"doc_code": "MEDICAL_REPORT", "named_person": "Diana Prince"},
        files={
            "file": ("med_report.pdf", b"%PDF-1.4 confidential medical record", "application/pdf")
        },
    )
    doc_id = upload_res.json()["id"]

    # 1. Staff (even if registered_by) is denied access to download sensitive medical document (403)
    claim.registered_by = user_staff.id
    db_session.commit()
    res_staff_dl = client.get(
        f"/api/claims/{claim.id}/documents/{doc_id}/download",
        headers=hdr_staff,
    )
    assert res_staff_dl.status_code == 403
    assert "Access to sensitive document is restricted" in res_staff_dl.json()["detail"]

    # 2. Assessor has authorized access to download
    res_assessor_dl = client.get(
        f"/api/claims/{claim.id}/documents/{doc_id}/download",
        headers=hdr_assessor,
    )
    assert res_assessor_dl.status_code == 200
    assert res_assessor_dl.content == b"%PDF-1.4 confidential medical record"

    # 3. Verify sensitive document download wrote audit log entry
    audit = (
        db_session.query(AuditLog)
        .filter_by(chain_key=f"claim:{claim.id}", action="SENSITIVE_DOCUMENT_DOWNLOADED")
        .first()
    )
    assert audit is not None
    assert audit.actor_role == "assessor"

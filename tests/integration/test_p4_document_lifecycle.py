"""Comprehensive integration test suite for Phase P4: Document Lifecycle & Identity Attestation (Decisions D4 & D8).

Covers:
1. Document replacement and superseding history (attestations never carry over).
2. Illegal document review transitions (cannot review superseded, cannot set SUPERSEDED).
3. Append-only revision table linearity (sequential rev numbers, prev_rev = rev - 1, DB check constraints).
4. Concurrency conflict detection (stale expected_rev returns 409).
5. Identity attestation stale detection when customer identity_version increments.
6. Separation of duties: uploader cannot review or attest their own uploaded document.
7. Role matrix: staff and customer cannot review or attest (D8: assessor & manager only).
8. INVALID and MISMATCH validation constraints (mandatory reason_code and remarks).
9. Transactional revalidation on upload, replacement, review, and attestation.
10. R08 risk indicator isolation (ignores invalid docs and stale attestations).
11. Stale claims report strictly read-only verification.
"""

import hashlib
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.models import (
    AuditLog,
    Claim,
    ClaimDocument,
    ClaimStatusHistory,
    Coverage,
    Customer,
    DocumentIdentityAttestation,
    Policy,
    RequiredDocumentType,
    User,
)
from app.domain.enums import (
    AttestationResult,
    ClaimChannel,
    ClaimStatus,
    DocumentStatus,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.engine.risk import evaluate_risk
from app.engine.types import DocumentInput
from app.security.password import hash_password
from app.security.session import create_session
from scripts.stale_claims_report import get_stale_claims
from tests.engine.test_risk_engine import make_risk_input


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def p4_setup(db_session: Session) -> dict[str, Any]:
    """Setup users across all roles, customer, active policy, coverage, and required doc types."""
    customer = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-P4-001",
        full_name="Diana Prince",
        dob=date(1988, 3, 15),
        email="diana.p4@example.com",
        phone="+15550044",
        address="10 Paradise Island",
        state_region="CA",
        identity_version=1,
    )
    user_cust = User(
        id=uuid.uuid4(),
        email="diana.p4@example.com",
        password_hash=hash_password("DianaSecurePass123!"),
        role=UserRole.CUSTOMER,
        customer_id=customer.id,
        is_active=True,
    )
    user_staff = User(
        id=uuid.uuid4(),
        email="staff.p4@example.com",
        password_hash=hash_password("StaffPass123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_assessor1 = User(
        id=uuid.uuid4(),
        email="assessor1.p4@example.com",
        password_hash=hash_password("AssessorPass123!"),
        role=UserRole.ASSESSOR,
        is_active=True,
    )
    user_assessor2 = User(
        id=uuid.uuid4(),
        email="assessor2.p4@example.com",
        password_hash=hash_password("AssessorPass123!"),
        role=UserRole.ASSESSOR,
        is_active=True,
    )
    user_manager = User(
        id=uuid.uuid4(),
        email="manager.p4@example.com",
        password_hash=hash_password("ManagerPass123!"),
        role=UserRole.MANAGER,
        is_active=True,
    )

    db_session.add_all(
        [customer, user_cust, user_staff, user_assessor1, user_assessor2, user_manager]
    )
    db_session.flush()

    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-P4-001",
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

    # Required document types: one identity-bearing (DOC_ID), one non-identity (DOC_MED)
    req_id = RequiredDocumentType(
        id=uuid.uuid4(),
        product_type=ProductType.HEALTH,
        claim_category="HOSPITALIZATION",
        doc_code="DOC_ID",
        name="Government Photo ID",
        is_mandatory=True,
        is_identity_bearing=True,
    )
    req_med = RequiredDocumentType(
        id=uuid.uuid4(),
        product_type=ProductType.HEALTH,
        claim_category="HOSPITALIZATION",
        doc_code="DOC_MED",
        name="Hospital Medical Discharge Summary",
        is_mandatory=True,
        is_identity_bearing=False,
    )
    db_session.add_all([req_id, req_med])
    db_session.flush()

    # Claim in DOCUMENTS_PENDING
    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-P4-000101",
        policy_id=policy.id,
        customer_id=customer.id,
        claim_category="HOSPITALIZATION",
        incident_date=date.today() - timedelta(days=10),
        incident_location="General Hospital",
        incident_description="Emergency appendectomy.",
        requested_amount=Decimal("50000.00"),
        registered_by=user_cust.id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.DOCUMENTS_PENDING,
        status_version=1,
    )
    db_session.add(claim)
    db_session.commit()

    return {
        "customer": customer,
        "user_cust": user_cust,
        "user_staff": user_staff,
        "user_assessor1": user_assessor1,
        "user_assessor2": user_assessor2,
        "user_manager": user_manager,
        "policy": policy,
        "coverage": coverage,
        "claim": claim,
    }


def test_document_replacement_and_superseding_history(
    client: TestClient, db_session: Session, p4_setup: dict[str, Any]
) -> None:
    """Replacing an active document marks old row SUPERSEDED, audits event, and never carries attestations."""
    user_cust = p4_setup["user_cust"]
    user_assessor1 = p4_setup["user_assessor1"]
    claim = p4_setup["claim"]

    hdr_cust = auth_headers(db_session, user_cust)
    hdr_assessor = auth_headers(db_session, user_assessor1)

    # 1. Upload original document (v1)
    file_bytes_v1 = b"%PDF-1.4 ID Card Original Version"
    res1 = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_cust,
        data={"doc_code": "DOC_ID"},
        files={"file": ("id_v1.pdf", file_bytes_v1, "application/pdf")},
    )
    assert res1.status_code == 201
    doc_v1_id = res1.json()["id"]
    doc_v1_sha = hashlib.sha256(file_bytes_v1).hexdigest()
    assert res1.json()["status"] == "PENDING_REVIEW"
    assert res1.json()["sha256"] == doc_v1_sha

    # 2. Assessor reviews and attests identity on v1
    res_att1 = client.post(
        f"/api/claims/{claim.id}/documents/{doc_v1_id}/attestations",
        headers=hdr_assessor,
        json={"result": "CONFIRMED", "expected_rev": 0},
    )
    assert res_att1.status_code == 201
    assert res_att1.json()["rev"] == 1
    assert res_att1.json()["prev_rev"] is None
    assert res_att1.json()["result"] == "CONFIRMED"

    # 3. Customer uploads replacement document (v2) with same doc_code
    file_bytes_v2 = b"%PDF-1.4 ID Card Replaced Version With Better Resolution"
    res2 = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_cust,
        data={"doc_code": "DOC_ID"},
        files={"file": ("id_v2.pdf", file_bytes_v2, "application/pdf")},
    )
    assert res2.status_code == 201
    doc_v2_id = res2.json()["id"]
    doc_v2_sha = hashlib.sha256(file_bytes_v2).hexdigest()
    assert doc_v2_id != doc_v1_id
    assert res2.json()["status"] == "PENDING_REVIEW"
    assert res2.json()["sha256"] == doc_v2_sha

    # 4. Verify v1 is marked SUPERSEDED in database
    db_session.expire_all()
    doc_v1 = db_session.get(ClaimDocument, uuid.UUID(doc_v1_id))
    assert doc_v1 is not None
    assert doc_v1.status == DocumentStatus.SUPERSEDED

    # Verify audit event for DOCUMENT_SUPERSEDED
    audit = db_session.scalars(
        select(AuditLog).where(
            AuditLog.chain_key == f"claim:{claim.id}",
            AuditLog.action == "DOCUMENT_SUPERSEDED",
        )
    ).first()
    assert audit is not None
    assert audit.event_data["old_document_id"] == doc_v1_id
    assert audit.event_data["document_sha256"] == doc_v1_sha

    # 5. Attestations never carry over: v2 has 0 attestations
    res_v2_att = client.get(
        f"/api/claims/{claim.id}/documents/{doc_v2_id}/attestations",
        headers=hdr_assessor,
    )
    assert res_v2_att.status_code == 200
    assert len(res_v2_att.json()) == 0

    # 6. Illegal transitions: cannot review or attest superseded document
    res_bad_review = client.patch(
        f"/api/claims/{claim.id}/documents/{doc_v1_id}/review",
        headers=hdr_assessor,
        json={"status": "VALID"},
    )
    assert res_bad_review.status_code == 400
    assert "superseded" in res_bad_review.json()["detail"].lower()

    res_bad_attest = client.post(
        f"/api/claims/{claim.id}/documents/{doc_v1_id}/attestations",
        headers=hdr_assessor,
        json={"result": "CONFIRMED", "expected_rev": 1},
    )
    assert res_bad_attest.status_code == 400
    assert "superseded" in res_bad_attest.json()["detail"].lower()


def test_identity_attestation_revision_linearity_and_concurrency(
    client: TestClient, db_session: Session, p4_setup: dict[str, Any]
) -> None:
    """Attestation revisions are strictly linear; concurrent stale expected_rev yields 409 Conflict."""
    user_cust = p4_setup["user_cust"]
    user_assessor1 = p4_setup["user_assessor1"]
    user_assessor2 = p4_setup["user_assessor2"]
    claim = p4_setup["claim"]

    hdr_cust = auth_headers(db_session, user_cust)
    hdr_ass1 = auth_headers(db_session, user_assessor1)
    hdr_ass2 = auth_headers(db_session, user_assessor2)

    # Upload document
    upload_res = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_cust,
        data={"doc_code": "DOC_ID"},
        files={"file": ("passport.pdf", b"%PDF-1.4 Valid Passport", "application/pdf")},
    )
    doc_id = upload_res.json()["id"]

    # Revision 1: initial attestation UNCERTAIN
    res1 = client.post(
        f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
        headers=hdr_ass1,
        json={
            "result": "UNCERTAIN",
            "reason_code": "IMAGE_GLARE",
            "remarks": "Glare on hologram requires secondary inspection",
            "expected_rev": 0,
        },
    )
    assert res1.status_code == 201
    data1 = res1.json()
    assert data1["rev"] == 1
    assert data1["prev_rev"] is None
    assert data1["result"] == "UNCERTAIN"
    assert data1["reviewer_id"] == str(user_assessor1.id)
    assert data1["reviewer_role"].lower() == "assessor"

    # Concurrency test: Assessor 2 and Assessor 1 both see rev 1.
    # Assessor 1 posts revision 2 first -> succeeds
    res2 = client.post(
        f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
        headers=hdr_ass1,
        json={
            "result": "CONFIRMED",
            "expected_rev": 1,
        },
    )
    assert res2.status_code == 201
    data2 = res2.json()
    assert data2["rev"] == 2
    assert data2["prev_rev"] == 1
    assert data2["result"] == "CONFIRMED"

    # Assessor 2 attempts to post with stale expected_rev=1 -> 409 Conflict
    res_conflict = client.post(
        f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
        headers=hdr_ass2,
        json={
            "result": "MISMATCH",
            "reason_code": "NAME_MISMATCH",
            "remarks": "Conflict detected during review",
            "expected_rev": 1,
        },
    )
    assert res_conflict.status_code == 409
    assert "revision conflict" in res_conflict.json()["detail"].lower()

    # Assessor 2 updates expected_rev to 2 -> succeeds as rev 3
    res3 = client.post(
        f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
        headers=hdr_ass2,
        json={
            "result": "CONFIRMED",
            "expected_rev": 2,
        },
    )
    assert res3.status_code == 201
    assert res3.json()["rev"] == 3
    assert res3.json()["prev_rev"] == 2

    # Query all revisions
    hist_res = client.get(
        f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
        headers=hdr_ass1,
    )
    assert hist_res.status_code == 200
    revs = hist_res.json()
    assert len(revs) == 3
    assert [r["rev"] for r in revs] == [1, 2, 3]
    assert [r["prev_rev"] for r in revs] == [None, 1, 2]
    # Highest revision is marked is_current=True, older are is_current=False
    assert [r["is_current"] for r in revs] == [False, False, True]


def test_db_level_attestation_sequence_check_constraint(
    db_session: Session, p4_setup: dict[str, Any]
) -> None:
    """Database mechanically rejects non-linear revisions and gaps."""
    user_cust = p4_setup["user_cust"]
    user_assessor = p4_setup["user_assessor1"]
    claim = p4_setup["claim"]

    doc = ClaimDocument(
        id=uuid.uuid4(),
        claim_id=claim.id,
        doc_code="DOC_ID",
        file_name="test.pdf",
        storage_key="test_key_seq",
        sha256="abc1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        size_bytes=100,
        mime_type="application/pdf",
        status=DocumentStatus.PENDING_REVIEW,
        uploaded_by=user_cust.id,
        created_at=utc_now(),
    )
    db_session.add(doc)
    db_session.flush()

    # Attempt to insert rev=2 with prev_rev=None (illegal: rev > 1 must have prev_rev = rev - 1)
    bad_att = DocumentIdentityAttestation(
        document_id=doc.id,
        rev=2,
        prev_rev=None,
        document_sha256=doc.sha256,
        result=AttestationResult.CONFIRMED,
        reviewer_id=user_assessor.id,
        reviewer_role="ASSESSOR",
        subject_identity_version=1,
        created_at=utc_now(),
    )
    db_session.add(bad_att)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_identity_attestation_stale_detection_on_customer_version_increment(
    client: TestClient, db_session: Session, p4_setup: dict[str, Any]
) -> None:
    """When customer identity_version changes, prior attestations become stale and V13 blocks."""
    user_cust = p4_setup["user_cust"]
    user_assessor = p4_setup["user_assessor1"]
    customer = p4_setup["customer"]
    claim = p4_setup["claim"]

    hdr_cust = auth_headers(db_session, user_cust)
    hdr_ass = auth_headers(db_session, user_assessor)

    # Upload ID document and medical document
    res_id = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_cust,
        data={"doc_code": "DOC_ID"},
        files={"file": ("id.pdf", b"%PDF-1.4 ID Valid", "application/pdf")},
    )
    doc_id_id = res_id.json()["id"]

    res_med = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_cust,
        data={"doc_code": "DOC_MED"},
        files={"file": ("med.pdf", b"%PDF-1.4 Med Summary", "application/pdf")},
    )
    doc_med_id = res_med.json()["id"]

    # Assessor reviews ID doc to VALID and attests CONFIRMED (medical doc remains PENDING_REVIEW)
    client.patch(
        f"/api/claims/{claim.id}/documents/{doc_id_id}/review",
        headers=hdr_ass,
        json={"status": "VALID"},
    )
    client.post(
        f"/api/claims/{claim.id}/documents/{doc_id_id}/attestations",
        headers=hdr_ass,
        json={"result": "CONFIRMED", "expected_rev": 0},
    )

    # Claim remains in DOCUMENTS_PENDING because medical doc is still PENDING_REVIEW
    db_session.expire_all()
    claim_db = db_session.get(Claim, claim.id)
    assert claim_db is not None
    assert claim_db.status == ClaimStatus.DOCUMENTS_PENDING

    # Verify attestation is not stale
    doc_resp = client.get(f"/api/claims/{claim.id}/documents", headers=hdr_ass).json()
    id_doc_meta = next(d for d in doc_resp if d["id"] == doc_id_id)
    assert id_doc_meta["is_identity_bearing"] is True
    assert id_doc_meta["latest_attestation"]["is_current"] is True
    assert id_doc_meta["latest_attestation"]["is_stale"] is False

    # Customer updates profile / identity version increments to 2
    customer_db = db_session.get(Customer, customer.id)
    assert customer_db is not None
    customer_db.identity_version = 2
    customer_db.full_name = "Diana Prince (Updated)"
    db_session.flush()
    db_session.commit()

    # Re-reading documents: latest_attestation is now stale!
    doc_resp_stale = client.get(f"/api/claims/{claim.id}/documents", headers=hdr_ass).json()
    id_doc_meta_stale = next(d for d in doc_resp_stale if d["id"] == doc_id_id)
    assert id_doc_meta_stale["latest_attestation"]["is_stale"] is True

    # Revalidation from assessor: V13 requires current non-stale attestation!
    # Because attestation is stale, V13 returns BLOCKED_DOCS and claim remains in DOCUMENTS_PENDING
    res_val = client.post(f"/api/claims/{claim.id}/validate", headers=hdr_ass)
    assert res_val.status_code == 200
    assert res_val.json()["overall_result"] == "BLOCKED_DOCS"

    db_session.expire_all()
    claim_reval = db_session.get(Claim, claim.id)
    assert claim_reval is not None
    assert claim_reval.status == ClaimStatus.DOCUMENTS_PENDING

    # Assessor submits fresh attestation with subject_identity_version=2 and reviews medical doc to VALID
    client.post(
        f"/api/claims/{claim.id}/documents/{doc_id_id}/attestations",
        headers=hdr_ass,
        json={"result": "CONFIRMED", "expected_rev": 1},
    )
    client.patch(
        f"/api/claims/{claim.id}/documents/{doc_med_id}/review",
        headers=hdr_ass,
        json={"status": "VALID"},
    )

    # Claim is now fully valid and revalidates to VALIDATED
    db_session.expire_all()
    claim_final = db_session.get(Claim, claim.id)
    assert claim_final is not None
    assert claim_final.status == ClaimStatus.VALIDATED


def test_separation_of_duties_uploader_cannot_review_or_attest(
    client: TestClient, db_session: Session, p4_setup: dict[str, Any]
) -> None:
    """Manager who uploads a document cannot review or attest their own uploaded document."""
    user_mgr = p4_setup["user_manager"]
    user_ass2 = p4_setup["user_assessor2"]
    claim = p4_setup["claim"]

    hdr_mgr = auth_headers(db_session, user_mgr)
    hdr_ass2 = auth_headers(db_session, user_ass2)

    # Manager uploads a document
    upload_res = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_mgr,
        data={"doc_code": "DOC_ID"},
        files={"file": ("id_upload.pdf", b"%PDF-1.4 Uploaded By Manager", "application/pdf")},
    )
    assert upload_res.status_code == 201
    doc_id = upload_res.json()["id"]

    # Manager attempts to review their own uploaded doc -> 403 Forbidden
    res_self_review = client.patch(
        f"/api/claims/{claim.id}/documents/{doc_id}/review",
        headers=hdr_mgr,
        json={"status": "VALID"},
    )
    assert res_self_review.status_code == 403
    assert "uploader" in res_self_review.json()["detail"].lower()

    # Manager attempts to attest their own uploaded doc -> 403 Forbidden
    res_self_attest = client.post(
        f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
        headers=hdr_mgr,
        json={"result": "CONFIRMED", "expected_rev": 0},
    )
    assert res_self_attest.status_code == 403
    assert "uploader" in res_self_attest.json()["detail"].lower()

    # Assessor 2 (different user) reviews and attests -> 200/201 OK
    res_other_review = client.patch(
        f"/api/claims/{claim.id}/documents/{doc_id}/review",
        headers=hdr_ass2,
        json={"status": "VALID"},
    )
    assert res_other_review.status_code == 200

    res_other_attest = client.post(
        f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
        headers=hdr_ass2,
        json={"result": "CONFIRMED", "expected_rev": 0},
    )
    assert res_other_attest.status_code == 201


def test_role_matrix_for_document_review_and_attestation(
    client: TestClient, db_session: Session, p4_setup: dict[str, Any]
) -> None:
    """Staff and customers cannot review or attest documents (D8: assessor & manager only)."""
    user_cust = p4_setup["user_cust"]
    user_staff = p4_setup["user_staff"]
    user_manager = p4_setup["user_manager"]
    claim = p4_setup["claim"]

    hdr_cust = auth_headers(db_session, user_cust)
    hdr_staff = auth_headers(db_session, user_staff)
    hdr_mgr = auth_headers(db_session, user_manager)

    upload_res = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_cust,
        data={"doc_code": "DOC_ID"},
        files={"file": ("doc_matrix.pdf", b"%PDF-1.4 Matrix Test", "application/pdf")},
    )
    doc_id = upload_res.json()["id"]

    # Customer forbidden
    assert (
        client.patch(
            f"/api/claims/{claim.id}/documents/{doc_id}/review",
            headers=hdr_cust,
            json={"status": "VALID"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
            headers=hdr_cust,
            json={"result": "CONFIRMED", "expected_rev": 0},
        ).status_code
        == 403
    )

    # Staff forbidden
    assert (
        client.patch(
            f"/api/claims/{claim.id}/documents/{doc_id}/review",
            headers=hdr_staff,
            json={"status": "VALID"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
            headers=hdr_staff,
            json={"result": "CONFIRMED", "expected_rev": 0},
        ).status_code
        == 403
    )

    # Manager permitted
    assert (
        client.patch(
            f"/api/claims/{claim.id}/documents/{doc_id}/review",
            headers=hdr_mgr,
            json={"status": "VALID"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
            headers=hdr_mgr,
            json={"result": "CONFIRMED", "expected_rev": 0},
        ).status_code
        == 201
    )


def test_invalid_and_mismatch_require_reason_code_and_remarks(
    client: TestClient, db_session: Session, p4_setup: dict[str, Any]
) -> None:
    """Document INVALID status and attestation MISMATCH/UNCERTAIN require reason_code and remarks."""
    user_cust = p4_setup["user_cust"]
    user_ass = p4_setup["user_assessor1"]
    claim = p4_setup["claim"]

    hdr_cust = auth_headers(db_session, user_cust)
    hdr_ass = auth_headers(db_session, user_ass)

    upload_res = client.post(
        f"/api/claims/{claim.id}/documents",
        headers=hdr_cust,
        data={"doc_code": "DOC_ID"},
        files={"file": ("doc_val.pdf", b"%PDF-1.4 Validation Test", "application/pdf")},
    )
    doc_id = upload_res.json()["id"]

    # Review INVALID without reason_code -> 422
    res_inv_no_code = client.patch(
        f"/api/claims/{claim.id}/documents/{doc_id}/review",
        headers=hdr_ass,
        json={"status": "INVALID", "review_remarks": "Document is illegible"},
    )
    assert res_inv_no_code.status_code == 422

    # Review INVALID without review_remarks -> 422
    res_inv_no_rem = client.patch(
        f"/api/claims/{claim.id}/documents/{doc_id}/review",
        headers=hdr_ass,
        json={"status": "INVALID", "reason_code": "ILLEGIBLE"},
    )
    assert res_inv_no_rem.status_code == 422

    # Attestation MISMATCH without reason_code -> 422
    res_mis_no_code = client.post(
        f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
        headers=hdr_ass,
        json={
            "result": "MISMATCH",
            "remarks": "Name on document does not match",
            "expected_rev": 0,
        },
    )
    assert res_mis_no_code.status_code == 422

    # Attestation UNCERTAIN without remarks -> 422
    res_unc_no_rem = client.post(
        f"/api/claims/{claim.id}/documents/{doc_id}/attestations",
        headers=hdr_ass,
        json={"result": "UNCERTAIN", "reason_code": "UNCONFIRMED_SEAL", "expected_rev": 0},
    )
    assert res_unc_no_rem.status_code == 422


def test_r08_isolation_from_invalid_documents_and_stale_attestations() -> None:
    """R08 only triggers on VALID documents with current MISMATCH attestations."""
    # 1. VALID doc + current MISMATCH -> Triggered
    valid_mismatch = (
        DocumentInput(
            document_id="doc-1",
            doc_code="DOC_ID",
            status="VALID",
            is_identity_bearing=True,
            is_identity_attestation_current=True,
            identity_attestation_result="MISMATCH",
        ),
    )
    inp1 = make_risk_input(documents=valid_mismatch)
    res1 = next(r for r in evaluate_risk(inp1).indicator_results if r.indicator_id == "R08")
    assert res1.triggered
    assert res1.weight_applied == 20

    # 2. INVALID doc + MISMATCH -> Untriggered (untrusted metadata)
    invalid_mismatch = (
        DocumentInput(
            document_id="doc-2",
            doc_code="DOC_ID",
            status="INVALID",
            is_identity_bearing=True,
            is_identity_attestation_current=True,
            identity_attestation_result="MISMATCH",
        ),
    )
    inp2 = make_risk_input(documents=invalid_mismatch)
    res2 = next(r for r in evaluate_risk(inp2).indicator_results if r.indicator_id == "R08")
    assert not res2.triggered

    # 3. VALID doc + STALE MISMATCH (is_identity_attestation_current=False) -> Untriggered
    stale_mismatch = (
        DocumentInput(
            document_id="doc-3",
            doc_code="DOC_ID",
            status="VALID",
            is_identity_bearing=True,
            is_identity_attestation_current=False,
            identity_attestation_result="MISMATCH",
        ),
    )
    inp3 = make_risk_input(documents=stale_mismatch)
    res3 = next(r for r in evaluate_risk(inp3).indicator_results if r.indicator_id == "R08")
    assert not res3.triggered


def test_stale_claims_report_is_strictly_read_only(
    db_session: Session, p4_setup: dict[str, Any]
) -> None:
    """scripts/stale_claims_report.py generates reports without mutating any claim or status."""
    customer = p4_setup["customer"]
    policy = p4_setup["policy"]
    user_cust = p4_setup["user_cust"]

    now = utc_now()

    # Claim 1: 45 days old, DOCUMENTS_PENDING (stale)
    c1 = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-STALE-001",
        policy_id=policy.id,
        customer_id=customer.id,
        claim_category="HOSPITALIZATION",
        incident_date=(now - timedelta(days=50)).date(),
        incident_location="Hospital",
        incident_description="Stale claim test 1",
        requested_amount=Decimal("15000.00"),
        registered_by=user_cust.id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.DOCUMENTS_PENDING,
        status_version=1,
        registered_at=now - timedelta(days=45),
    )
    # Claim 2: 10 days old, REGISTERED (not stale)
    c2 = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-STALE-002",
        policy_id=policy.id,
        customer_id=customer.id,
        claim_category="HOSPITALIZATION",
        incident_date=(now - timedelta(days=15)).date(),
        incident_location="Hospital",
        incident_description="Fresh claim test 2",
        requested_amount=Decimal("20000.00"),
        registered_by=user_cust.id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.REGISTERED,
        status_version=1,
        registered_at=now - timedelta(days=10),
    )
    # Claim 3: 60 days old, APPROVED (terminal: must not appear in stale report)
    c3 = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-STALE-003",
        policy_id=policy.id,
        customer_id=customer.id,
        claim_category="HOSPITALIZATION",
        incident_date=(now - timedelta(days=70)).date(),
        incident_location="Hospital",
        incident_description="Terminal claim test 3",
        requested_amount=Decimal("35000.00"),
        registered_by=user_cust.id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.APPROVED,
        status_version=2,
        registered_at=now - timedelta(days=60),
    )
    db_session.add_all([c1, c2, c3])
    db_session.commit()

    # Snapshot status and history count before report
    status_before_c1 = c1.status
    hist_count_before = db_session.query(ClaimStatusHistory).count()

    # Run report with 30-day threshold
    report = get_stale_claims(db=db_session, threshold_days=30)

    # Assertions on report output
    stale_refs = [item["claim_ref"] for item in report]
    assert "CLM-STALE-001" in stale_refs
    assert "CLM-STALE-002" not in stale_refs
    assert "CLM-STALE-003" not in stale_refs

    # Verify no state was mutated
    db_session.expire_all()
    c1_after = db_session.get(Claim, c1.id)
    assert c1_after is not None
    assert c1_after.status == status_before_c1
    assert c1_after.status_version == 1

    hist_count_after = db_session.query(ClaimStatusHistory).count()
    assert hist_count_after == hist_count_before

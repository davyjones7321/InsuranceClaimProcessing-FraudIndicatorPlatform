"""Comprehensive test suite for Phase P8: Product Surface, Deployment Artifacts, and Acceptance.

Covers:
1. Server-rendered Jinja2 UI endpoints (/ui/*) and role authorization (Decision D2).
2. Customer-safe status masking (UNDER_INVESTIGATION and ESCALATED -> UNDER_REVIEW) (Decisions D6, D7).
3. Zero risk data leakage to unauthorized roles (Customer and Staff) (Decision D6).
4. Restricted risk visibility for Assessor (category and completeness only) (Decision D6).
5. Full risk visibility with mandatory pilot banner for Investigator and Manager (Decision D6).
6. Production lifespan configuration validation (DEBUG, secrets, ruleset hash) (P8 Task 4).
7. Healthz, Readyz, and Metrics probes (P8 Task 3).
8. Separation of duties enforcement in UI actions (Decision D8, P1 task 8).
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    Claim,
    ClaimDocument,
    Coverage,
    Customer,
    Investigation,
    Policy,
    RiskEvaluation,
    RiskIndicatorResult,
    User,
)
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    DocumentStatus,
    EvidenceLevel,
    IndicatorEvaluationStatus,
    InvestigationStatus,
    LimitScope,
    PolicyStatus,
    ProductType,
    RiskCategory,
    RiskCompleteness,
    UserRole,
)
from app.main import app, lifespan
from app.rules.loader import ACTIVE_RULESET_SHA256
from app.security.csrf import generate_csrf_token
from app.security.password import hash_password
from app.security.session import create_session


@pytest.fixture
def test_users(db_session: Session) -> dict[str, User]:
    """Create test users representing all system roles."""
    roles = {
        "customer_1": (UserRole.CUSTOMER, "cust1@example.com"),
        "customer_2": (UserRole.CUSTOMER, "cust2@example.com"),
        "staff": (UserRole.STAFF, "staff@example.com"),
        "assessor": (UserRole.ASSESSOR, "assessor@example.com"),
        "investigator": (UserRole.INVESTIGATOR, "investigator@example.com"),
        "decision_maker": (UserRole.STAFF, "decision@example.com"),
        "manager": (UserRole.MANAGER, "manager@example.com"),
        "admin": (UserRole.ADMIN, "admin@example.com"),
    }
    users = {}
    for key, (role, email) in roles.items():
        existing = db_session.scalars(select(User).where(User.email == email)).first()
        if existing:
            users[key] = existing
            continue
        user = User(
            id=uuid.uuid4(),
            email=email,
            password_hash=hash_password("Password123!"),
            role=role,
            is_active=True,
        )
        db_session.add(user)
        users[key] = user
    db_session.commit()

    # Link customer_1 and customer_2 to customer records
    c1 = db_session.scalars(select(Customer).where(Customer.email == "cust1@example.com")).first()
    if not c1:
        c1 = Customer(
            id=uuid.uuid4(),
            customer_no="CUST-P8-001",
            full_name="Customer One",
            dob=date(1990, 1, 1),
            email="cust1@example.com",
            phone="+919876543210",
            address="123 Marine Drive",
            state_region="Maharashtra",
        )
        db_session.add(c1)
        db_session.flush()
    users["customer_1"].customer_id = c1.id

    c2 = db_session.scalars(select(Customer).where(Customer.email == "cust2@example.com")).first()
    if not c2:
        c2 = Customer(
            id=uuid.uuid4(),
            customer_no="CUST-P8-002",
            full_name="Customer Two",
            dob=date(1992, 5, 12),
            email="cust2@example.com",
            phone="+919876543211",
            address="456 Park Street",
            state_region="West Bengal",
        )
        db_session.add(c2)
        db_session.flush()
    users["customer_2"].customer_id = c2.id

    db_session.commit()
    return users


@pytest.fixture
def sample_claim_environment(db_session: Session, test_users: dict[str, User]) -> dict[str, Any]:
    """Create a policy, coverage, and claim for UI workflow tests."""
    cust = db_session.scalars(
        select(Customer).where(Customer.id == test_users["customer_1"].customer_id)
    ).first()
    assert cust is not None

    policy = Policy(
        id=uuid.uuid4(),
        policy_no=f"POL-P8-{uuid.uuid4().hex[:6]}",
        customer_id=cust.id,
        product_type=ProductType.HEALTH,
        status=PolicyStatus.ACTIVE,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("15000.00"),
        sum_insured=Decimal("500000.00"),
    )
    db_session.add(policy)
    db_session.flush()

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="HOSPITALIZATION",
        limit_amount=Decimal("200000.00"),
        limit_scope=LimitScope.PER_CLAIM,
        deductible=Decimal("2000.00"),
        copay_percent=Decimal("10.00"),
        consumed_amount=Decimal("0.00"),
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)
    db_session.flush()

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-P8-{uuid.uuid4().hex[:6]}",
        customer_id=cust.id,
        policy_id=policy.id,
        claim_category="HOSPITALIZATION",
        channel=ClaimChannel.CUSTOMER,
        registered_by=test_users["customer_1"].id,
        status=ClaimStatus.UNDER_INVESTIGATION,
        status_version=1,
        incident_date=date(2026, 3, 1),
        incident_location="Mumbai",
        requested_amount=Decimal("50000.00"),
        incident_description="Emergency appendectomy hospitalization",
        registered_tz="Asia/Kolkata",
    )
    db_session.add(claim)
    db_session.flush()

    # Add risk evaluation
    risk_eval = RiskEvaluation(
        id=uuid.uuid4(),
        claim_id=claim.id,
        ruleset_version="v1",
        engine_version="1.0.0",
        evaluated_on=date(2026, 3, 1),
        score=35,
        category=RiskCategory.HIGH,
        completeness=RiskCompleteness.COMPLETE,
        requires_investigation=True,
        routing_reasons=["R01"],
        pilot_banner="UNVALIDATED PILOT FRAUD INDICATORS",
        group_scores={"FREQUENCY": 20, "AMOUNT": 15},
        inputs_snapshot={"category": "HOSPITALIZATION"},
    )
    db_session.add(risk_eval)
    db_session.flush()

    ind_result = RiskIndicatorResult(
        id=uuid.uuid4(),
        evaluation_id=risk_eval.id,
        indicator_id="R01",
        indicator_version="v1",
        group="FREQUENCY",
        weight=20,
        triggered=True,
        weight_applied=20,
        sufficient_alone=False,
        evidence_level=EvidenceLevel.REVIEWED,
        evidence={"incident_count": 3},
        message="3 distinct incidents in the last 12 months",
        status=IndicatorEvaluationStatus.TRIGGERED,
    )
    db_session.add(ind_result)

    # Assign investigator
    inv = Investigation(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assigned_to=test_users["investigator"].id,
        assigned_by=test_users["manager"].id,
        status=InvestigationStatus.ASSIGNED,
        referral_type="AUTOMATED",
    )
    db_session.add(inv)
    db_session.commit()

    return {
        "policy": policy,
        "coverage": coverage,
        "claim": claim,
        "risk_eval": risk_eval,
        "investigation": inv,
    }


def login_client(client: TestClient, db: Session, user: User) -> str:
    """Helper to authenticate client via cookie and generate valid CSRF token."""
    session_id = create_session(db, user)
    db.commit()
    client.cookies.set(settings.SESSION_COOKIE_NAME, session_id)
    csrf_token = generate_csrf_token(session_id)
    return csrf_token


# ==============================================================================
# UI Navigation and Role Authorization Tests
# ==============================================================================


def test_ui_root_redirect(client: TestClient) -> None:
    """Root URL redirects to /ui/claims."""
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/ui/claims"


def test_ui_unauthenticated_redirects_to_login(client: TestClient) -> None:
    """Unauthenticated access to protected UI routes redirects to login."""
    client.cookies.clear()
    res = client.get("/ui/claims", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/ui/login"


def test_ui_login_and_logout_flow(
    client: TestClient, db_session: Session, test_users: dict[str, User]
) -> None:
    """User can log in via HTML form and log out successfully."""
    client.cookies.clear()
    login_page = client.get("/ui/login")
    assert login_page.status_code == 200
    assert "Login" in login_page.text

    # Submit login
    post_res = client.post(
        "/ui/login",
        data={"email": "staff@example.com", "password": "Password123!"},
        follow_redirects=False,
    )
    assert post_res.status_code == 303
    assert post_res.headers["location"] == "/ui/claims"
    assert settings.SESSION_COOKIE_NAME in client.cookies

    # Logout
    session_id = client.cookies[settings.SESSION_COOKIE_NAME]
    csrf_token = generate_csrf_token(session_id)
    logout_res = client.post(
        "/ui/logout",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert logout_res.status_code == 303
    assert logout_res.headers["location"].startswith("/ui/login")


def test_ui_customer_safe_status_masking(
    client: TestClient,
    db_session: Session,
    test_users: dict[str, User],
    sample_claim_environment: dict[str, Any],
) -> None:
    """Customer sees UNDER_REVIEW instead of sensitive UNDER_INVESTIGATION status."""
    claim = sample_claim_environment["claim"]
    assert claim.status == ClaimStatus.UNDER_INVESTIGATION

    login_client(client, db_session, test_users["customer_1"])

    # 1. Claims list
    list_res = client.get("/ui/claims")
    assert list_res.status_code == 200
    assert "UNDER_REVIEW" in list_res.text
    assert "UNDER_INVESTIGATION" not in list_res.text

    # 2. Claim detail
    detail_res = client.get(f"/ui/claims/{claim.id}")
    assert detail_res.status_code == 200
    assert "UNDER_REVIEW" in detail_res.text
    assert "UNDER_INVESTIGATION" not in detail_res.text


def test_ui_zero_risk_leakage_to_customer_and_staff(
    client: TestClient,
    db_session: Session,
    test_users: dict[str, User],
    sample_claim_environment: dict[str, Any],
) -> None:
    """Customer and Staff see zero risk data or indicator details (Decision D6)."""
    claim = sample_claim_environment["claim"]

    # Customer view
    login_client(client, db_session, test_users["customer_1"])
    cust_res = client.get(f"/ui/claims/{claim.id}")
    assert cust_res.status_code == 200
    assert "Risk Evaluation" not in cust_res.text
    assert "R01" not in cust_res.text
    assert "Pilot Fraud Indicators" not in cust_res.text

    # Staff view
    # Assign registration to staff to allow detail access
    claim.registered_by = test_users["staff"].id
    db_session.commit()

    login_client(client, db_session, test_users["staff"])
    staff_res = client.get(f"/ui/claims/{claim.id}")
    assert staff_res.status_code == 200
    assert "Risk Evaluation" not in staff_res.text
    assert "R01" not in staff_res.text


def test_ui_assessor_sees_risk_category_only(
    client: TestClient,
    db_session: Session,
    test_users: dict[str, User],
    sample_claim_environment: dict[str, Any],
) -> None:
    """Assessor sees only Risk Category and Completeness, no indicator trigger evidence."""
    claim = sample_claim_environment["claim"]
    login_client(client, db_session, test_users["assessor"])

    res = client.get(f"/ui/claims/{claim.id}")
    assert res.status_code == 200
    assert "Risk Band:" in res.text
    assert "HIGH" in res.text
    assert "COMPLETE" in res.text
    # Assessor must NOT see specific indicators or scores
    assert "R01" not in res.text
    assert "distinct incidents" not in res.text


def test_ui_investigator_and_manager_see_pilot_banner_and_indicators(
    client: TestClient,
    db_session: Session,
    test_users: dict[str, User],
    sample_claim_environment: dict[str, Any],
) -> None:
    """Investigator and Manager see mandatory unvalidated pilot banner and full indicators."""
    claim = sample_claim_environment["claim"]

    # Investigator view
    login_client(client, db_session, test_users["investigator"])
    inv_res = client.get(f"/ui/claims/{claim.id}")
    assert inv_res.status_code == 200
    assert "PILOT NOTICE" in inv_res.text
    assert "unvalidated pilot values" in inv_res.text
    assert "R01" in inv_res.text
    assert "3 distinct incidents in the last 12 months" in inv_res.text

    # Manager view
    login_client(client, db_session, test_users["manager"])
    mgr_res = client.get(f"/ui/claims/{claim.id}")
    assert mgr_res.status_code == 200
    assert "PILOT NOTICE" in mgr_res.text
    assert "unvalidated pilot values" in mgr_res.text
    assert "R01" in mgr_res.text


def test_ui_customer_registration_flow(
    client: TestClient,
    db_session: Session,
    test_users: dict[str, User],
    sample_claim_environment: dict[str, Any],
) -> None:
    """Customer can register a claim via web UI form."""
    policy = sample_claim_environment["policy"]
    csrf_token = login_client(client, db_session, test_users["customer_1"])

    form_page = client.get("/ui/claims/new")
    assert form_page.status_code == 200
    assert "Register Claim" in form_page.text

    post_res = client.post(
        "/ui/claims/new",
        data={
            "policy_id": str(policy.id),
            "claim_category": "HOSPITALIZATION",
            "incident_date": "2026-04-10",
            "incident_location": "Delhi General Hospital",
            "requested_amount": "25000.00",
            "incident_description": "Routine inpatient surgery procedure",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert post_res.status_code == 303
    assert "/ui/claims/" in post_res.headers["location"]


def test_ui_analytics_dashboard_access_and_zero_risk(
    client: TestClient,
    db_session: Session,
    test_users: dict[str, User],
) -> None:
    """Analytics dashboard accessible to Staff and Manager with zero risk data."""
    # Customer blocked
    login_client(client, db_session, test_users["customer_1"])
    cust_res = client.get("/ui/analytics")
    assert cust_res.status_code == 403

    # Staff allowed
    login_client(client, db_session, test_users["staff"])
    staff_res = client.get("/ui/analytics")
    assert staff_res.status_code == 200
    assert "Operational Analytics" in staff_res.text
    assert "Total Registered Claims" in staff_res.text
    assert "UNVALIDATED PILOT" not in staff_res.text
    assert "R01" not in staff_res.text
    assert "Overall Composite Score" not in staff_res.text


# ==============================================================================
# Production Lifespan Boot Validation Tests (P8 Task 4)
# ==============================================================================


@pytest.mark.asyncio
async def test_production_lifespan_refuses_debug_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan raises RuntimeError if DEBUG=True in production environment."""
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setattr(settings, "SECRET_KEY", "prod-secret-" + "x" * 32)
    monkeypatch.setattr(settings, "AUDIT_HMAC_KEY", "prod-audit-" + "x" * 32)

    with pytest.raises(RuntimeError, match="DEBUG must not be True in production"):
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_production_lifespan_refuses_weak_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan raises RuntimeError if secrets are short or contain dev prefixes."""
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "DEBUG", False)

    # Short secret
    monkeypatch.setattr(settings, "SECRET_KEY", "short-secret")
    monkeypatch.setattr(settings, "AUDIT_HMAC_KEY", "prod-audit-" + "x" * 32)
    with pytest.raises(RuntimeError, match="SECRET_KEY must be a strong secret"):
        async with lifespan(app):
            pass

    # Dev prefix in HMAC key
    monkeypatch.setattr(settings, "SECRET_KEY", "prod-secret-" + "x" * 32)
    monkeypatch.setattr(settings, "AUDIT_HMAC_KEY", "dev-hmac-key-" + "x" * 32)
    with pytest.raises(RuntimeError, match="AUDIT_HMAC_KEY must be a strong secret"):
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_lifespan_refuses_ruleset_hash_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan raises RuntimeError if active ruleset hash does not match registered hash."""
    from app.rules import loader

    monkeypatch.setattr(
        loader,
        "ACTIVE_RULESET_SHA256",
        "invalid-sha256-hash-value-00000000000000000000000000000000",
    )
    with pytest.raises(RuntimeError, match="does not match registered hash"):
        async with lifespan(app):
            pass


# ==============================================================================
# Observability Probes and Metrics Tests (P8 Task 3)
# ==============================================================================


def test_observability_healthz_and_readyz(client: TestClient) -> None:
    """Probes /healthz and /readyz return HTTP 200 with required component checks."""
    # Liveness probe
    h_res = client.get("/healthz")
    assert h_res.status_code == 200
    assert h_res.json()["status"] == "ok"

    # Readiness probe
    r_res = client.get("/readyz")
    assert r_res.status_code == 200
    data = r_res.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"
    assert data["ruleset_sha256"] == ACTIVE_RULESET_SHA256
    assert data["hmac_key_present"] is True


def test_observability_metrics_aggregation(client: TestClient) -> None:
    """Endpoint /metrics reports latency, request count, 5xx rate, and status breakdown."""
    res = client.get("/metrics")
    assert res.status_code == 200
    metrics = res.json()
    assert "total_requests" in metrics
    assert "error_5xx_count" in metrics
    assert "requests_per_second" in metrics
    assert "average_latency_ms" in metrics
    assert "claims_by_status" in metrics
    assert isinstance(metrics["claims_by_status"], dict)


# ==============================================================================
# End-to-End Role Lifecycle & Separation of Duties Tests via UI
# ==============================================================================


def test_ui_document_upload_and_four_eyes_review(
    client: TestClient,
    db_session: Session,
    test_users: dict[str, User],
    sample_claim_environment: dict[str, Any],
) -> None:
    """Customer uploads document, customer cannot review own upload, assessor reviews."""
    claim = sample_claim_environment["claim"]

    # 1. Customer uploads document
    csrf_token = login_client(client, db_session, test_users["customer_1"])
    doc_content = b"%PDF-1.4 test hospital discharge bill document"
    upload_res = client.post(
        f"/ui/claims/{claim.id}/documents/upload",
        data={
            "doc_code": "DISCHARGE_SUMMARY",
            "document_date": "2026-03-02",
            "named_person": "Customer One",
            "csrf_token": csrf_token,
        },
        files={"file": ("discharge_summary.pdf", doc_content, "application/pdf")},
        follow_redirects=False,
    )
    assert upload_res.status_code == 303

    doc = db_session.scalars(
        select(ClaimDocument).where(ClaimDocument.claim_id == claim.id)
    ).first()
    assert doc is not None
    assert doc.doc_code == "DISCHARGE_SUMMARY"
    assert doc.status == DocumentStatus.PENDING_REVIEW

    # 2. Customer attempts to review own document -> blocked by separation of duties (D8)
    cust_review_res = client.post(
        f"/ui/claims/{claim.id}/documents/{doc.id}/review",
        data={
            "review_status": "VALID",
            "remarks": "Attempting self-review",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    # Redirects with error message or blocked
    assert cust_review_res.status_code == 303
    assert "msg=" in cust_review_res.headers["location"]

    # Document remains PENDING_REVIEW
    db_session.refresh(doc)
    assert doc.status == DocumentStatus.PENDING_REVIEW

    # 3. Assessor reviews and attests document -> success
    assessor_csrf = login_client(client, db_session, test_users["assessor"])
    assessor_review_res = client.post(
        f"/ui/claims/{claim.id}/documents/{doc.id}/review",
        data={
            "review_status": "VALID",
            "remarks": "Discharge summary verified against hospital records",
            "csrf_token": assessor_csrf,
        },
        follow_redirects=False,
    )
    assert assessor_review_res.status_code == 303
    db_session.refresh(doc)
    assert doc.status == DocumentStatus.VALID


def test_ui_investigation_and_assessment_and_decision_lifecycle(
    client: TestClient,
    db_session: Session,
    test_users: dict[str, User],
    sample_claim_environment: dict[str, Any],
) -> None:
    """Full lifecycle: Investigation completed -> Assessment calculated -> Decision executed."""
    claim = sample_claim_environment["claim"]
    assert claim.status == ClaimStatus.UNDER_INVESTIGATION

    # 1. Investigator completes investigation
    inv_csrf = login_client(client, db_session, test_users["investigator"])
    inv_res = client.post(
        f"/ui/claims/{claim.id}/investigation",
        data={
            "outcome": "CLEARED",
            "findings_summary": "Inpatient hospitalization confirmed with attending surgeon. No anomalies.",
            "csrf_token": inv_csrf,
        },
        follow_redirects=False,
    )
    assert inv_res.status_code == 303
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.READY_FOR_ASSESSMENT

    # 2. Assessor records assessment
    assessor_csrf = login_client(client, db_session, test_users["assessor"])
    assess_res = client.post(
        f"/ui/claims/{claim.id}/assessment",
        data={
            "assessed_loss": "45000.00",
            "deductible": "2000.00",
            "remarks": "Loss assessed based on verified hospital bill itemized charges.",
            "csrf_token": assessor_csrf,
        },
        follow_redirects=False,
    )
    assert assess_res.status_code == 303
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.ASSESSED

    # 3. Assessor attempts to make decision on claim they assessed -> blocked by separation of duties
    dec_assessor_res = client.post(
        f"/ui/claims/{claim.id}/decision",
        data={
            "outcome": "APPROVED",
            "approved_amount": "38700.00",
            "remarks": "Attempting approval as the assessor who assessed it.",
            "csrf_token": assessor_csrf,
        },
        follow_redirects=False,
    )
    assert dec_assessor_res.status_code == 303
    assert "error" in dec_assessor_res.headers["location"]
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.ASSESSED

    # 4. Manager executes approval decision with mandatory remarks >= 20 chars
    mgr_csrf = login_client(client, db_session, test_users["manager"])
    dec_mgr_res = client.post(
        f"/ui/claims/{claim.id}/decision",
        data={
            "outcome": "APPROVED",
            "approved_amount": "38700.00",
            "remarks": "Approved within policy limits and validated assessment cap.",
            "csrf_token": mgr_csrf,
        },
        follow_redirects=False,
    )
    assert dec_mgr_res.status_code == 303
    assert "msg_type=success" in dec_mgr_res.headers["location"]
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.APPROVED

"""Integration tests for Phase P6: Risk Governance, Incident Groups, and Waivers."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import (
    Claim,
    Coverage,
    Customer,
    HistoryCoverage,
    Policy,
    User,
)
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    LimitScope,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.security.session import create_session
from app.services.decision_service import (
    DecisionPermissionError,
    DecisionService,
)


@pytest.fixture
def test_setup(db_session: Session):
    customer = Customer(
        id=uuid.uuid4(),
        customer_no=f"CUST-P6-{uuid.uuid4().hex[:6]}",
        full_name="Meera Sen",
        dob=date(1992, 4, 10),
        email=f"meera_{uuid.uuid4().hex[:6]}@example.com",
        phone="+919876543200",
        address="100 Salt Lake, Kolkata",
        state_region="West Bengal",
    )
    db_session.add(customer)

    policy = Policy(
        id=uuid.uuid4(),
        policy_no=f"POL-P6-{uuid.uuid4().hex[:6]}",
        customer_id=customer.id,
        product_type=ProductType.HEALTH,
        status=PolicyStatus.ACTIVE,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 1),
        premium=Decimal("15000.00"),
        sum_insured=Decimal("500000.00"),
    )
    db_session.add(policy)

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="INPATIENT",
        limit_scope=LimitScope.PER_POLICY_PERIOD,
        limit_amount=Decimal("500000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)

    assessor = User(
        id=uuid.uuid4(),
        email=f"assessor_p6_{uuid.uuid4().hex[:6]}@example.com",
        role=UserRole.ASSESSOR,
        password_hash="mock",
    )
    manager = User(
        id=uuid.uuid4(),
        email=f"manager_p6_{uuid.uuid4().hex[:6]}@example.com",
        role=UserRole.MANAGER,
        password_hash="mock",
    )
    investigator = User(
        id=uuid.uuid4(),
        email=f"inv_p6_{uuid.uuid4().hex[:6]}@example.com",
        role=UserRole.INVESTIGATOR,
        password_hash="mock",
    )
    staff = User(
        id=uuid.uuid4(),
        email=f"staff_p6_{uuid.uuid4().hex[:6]}@example.com",
        role=UserRole.STAFF,
        password_hash="mock",
    )
    db_session.add_all([assessor, manager, investigator, staff])
    db_session.commit()

    return {
        "customer": customer,
        "policy": policy,
        "coverage": coverage,
        "assessor": assessor,
        "manager": manager,
        "investigator": investigator,
        "staff": staff,
    }


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


def test_d6_role_specific_risk_visibility(
    client: TestClient, db_session: Session, test_setup: dict
):
    """Decision D6: Assessor gets category, completeness, composed_label only (no score, no evidence)."""
    assessor = test_setup["assessor"]
    manager = test_setup["manager"]
    customer = test_setup["customer"]
    policy = test_setup["policy"]

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-D6-{uuid.uuid4().hex[:4]}",
        customer_id=customer.id,
        policy_id=policy.id,
        incident_date=date(2025, 2, 1),
        claim_category="OUTPATIENT",
        incident_location="Kolkata",
        incident_description="Medical admission",
        requested_amount=Decimal("50000.00"),
        registered_by=assessor.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.VALIDATED,
    )
    db_session.add(claim)
    db_session.commit()

    # Manager evaluates risk
    eval_resp = client.post(
        f"/api/claims/{claim.id}/evaluate-risk",
        headers=auth_headers(db_session, manager),
    )
    assert eval_resp.status_code == 200, eval_resp.text
    mgr_data = eval_resp.json()
    assert mgr_data["score"] is not None
    assert len(mgr_data["indicator_results"]) > 0
    assert len(mgr_data["group_scores"]) > 0

    # Assessor view
    assessor_resp = client.get(
        f"/api/claims/{claim.id}/risk-evaluations/latest",
        headers=auth_headers(db_session, assessor),
    )
    assert assessor_resp.status_code == 200, assessor_resp.text
    assessor_data = assessor_resp.json()
    assert assessor_data["category"] is not None
    assert assessor_data["completeness"] is not None
    assert assessor_data["composed_label"] is not None
    assert assessor_data["score"] is None
    assert assessor_data["indicator_results"] == []
    assert assessor_data["group_scores"] == {}
    assert assessor_data["inputs_snapshot"] == {}
    assert assessor_data["routing_reasons"] == []


def test_manager_completeness_waiver_workflow(
    client: TestClient, db_session: Session, test_setup: dict
):
    """Claims in RISK_DATA_PENDING advance to READY_FOR_ASSESSMENT upon manager waiver."""
    manager = test_setup["manager"]
    assessor = test_setup["assessor"]
    customer = test_setup["customer"]
    policy = test_setup["policy"]

    # History coverage starting today -> makes lookbacks fail
    history_cov = HistoryCoverage(
        id=uuid.uuid4(),
        product_type=ProductType.HEALTH,
        loaded_from=date(2030, 1, 1),
        set_by_id=manager.id,
        evidence_reference="HIST-SET-01",
    )
    db_session.add(history_cov)

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-WAI-{uuid.uuid4().hex[:4]}",
        customer_id=customer.id,
        policy_id=policy.id,
        incident_date=date(2025, 2, 1),
        claim_category="OUTPATIENT",
        incident_location="Kolkata",
        incident_description="Medical claim",
        requested_amount=Decimal("30000.00"),
        registered_by=assessor.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.VALIDATED,
    )
    db_session.add(claim)
    db_session.commit()

    eval_resp = client.post(
        f"/api/claims/{claim.id}/evaluate-risk",
        headers=auth_headers(db_session, manager),
    )
    assert eval_resp.status_code == 200

    db_session.refresh(claim)
    assert claim.status == ClaimStatus.RISK_DATA_PENDING

    # Assessor cannot waive (403 Forbidden)
    waiver_assessor = client.post(
        f"/api/claims/{claim.id}/risk-waiver",
        headers=auth_headers(db_session, assessor),
        json={"remarks": "Manager waiver granted after documentary review of customer history."},
    )
    assert waiver_assessor.status_code == 403

    # Manager waives
    waiver_mgr = client.post(
        f"/api/claims/{claim.id}/risk-waiver",
        headers=auth_headers(db_session, manager),
        json={"remarks": "Manager waiver granted after documentary review of customer history."},
    )
    assert waiver_mgr.status_code == 200
    db_session.refresh(claim)
    assert claim.status == ClaimStatus.READY_FOR_ASSESSMENT


def test_manual_referral_and_investigator_feedback(
    client: TestClient, db_session: Session, test_setup: dict
):
    """Manual referral routes to UNDER_INVESTIGATION; structured feedback recorded at closure."""
    manager = test_setup["manager"]
    assessor = test_setup["assessor"]
    investigator = test_setup["investigator"]
    customer = test_setup["customer"]
    policy = test_setup["policy"]

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-MAN-{uuid.uuid4().hex[:4]}",
        customer_id=customer.id,
        policy_id=policy.id,
        incident_date=date(2025, 2, 1),
        claim_category="OUTPATIENT",
        incident_location="Kolkata",
        incident_description="Medical claim",
        requested_amount=Decimal("20000.00"),
        registered_by=assessor.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.READY_FOR_ASSESSMENT,
    )
    db_session.add(claim)
    db_session.commit()

    # Manual referral by assessor
    ref_resp = client.post(
        f"/api/claims/{claim.id}/refer-investigation",
        headers=auth_headers(db_session, assessor),
        json={
            "remarks": "Suspicious hospital billing records requiring detailed field investigation."
        },
    )
    assert ref_resp.status_code == 200, ref_resp.text
    inv_id = ref_resp.json()["id"]

    db_session.refresh(claim)
    assert claim.status == ClaimStatus.UNDER_INVESTIGATION

    # Assign investigator
    assign_resp = client.post(
        f"/api/claims/{claim.id}/investigations/assign",
        headers=auth_headers(db_session, manager),
        json={"investigator_id": str(investigator.id)},
    )
    assert assign_resp.status_code == 200

    # Record feedback by assigned investigator
    feedback_payload = {
        "feedback": {
            "R05": {
                "status": "HELPFUL",
                "notes": "Billing pattern confirmed policy timing anomaly.",
            },
            "R08": {
                "status": "MISLEADING",
                "notes": "Name spelling difference was a clerical typo.",
            },
        }
    }
    fb_resp = client.post(
        f"/api/investigations/{inv_id}/feedback",
        headers=auth_headers(db_session, investigator),
        json=feedback_payload,
    )
    assert fb_resp.status_code == 200, fb_resp.text
    inv_data = fb_resp.json()
    assert inv_data["indicator_feedback"]["R05"]["status"] == "HELPFUL"
    assert inv_data["indicator_feedback"]["R08"]["status"] == "MISLEADING"


def test_incident_group_governance_and_separation_of_duties(
    client: TestClient, db_session: Session, test_setup: dict
):
    """Four-eyes proposal/confirmation and confirmer cannot decide member claims (Decision D12, check 5c)."""
    assessor = test_setup["assessor"]
    manager = test_setup["manager"]
    customer = test_setup["customer"]
    policy = test_setup["policy"]

    claim1 = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-G1-{uuid.uuid4().hex[:4]}",
        customer_id=customer.id,
        policy_id=policy.id,
        incident_date=date(2025, 3, 1),
        claim_category="INPATIENT",
        incident_location="Kolkata",
        incident_description="Multi-car collision vehicle damage",
        requested_amount=Decimal("40000.00"),
        registered_by=assessor.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.READY_FOR_ASSESSMENT,
    )
    claim2 = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-G2-{uuid.uuid4().hex[:4]}",
        customer_id=customer.id,
        policy_id=policy.id,
        incident_date=date(2025, 3, 3),
        claim_category="INPATIENT",
        incident_location="Kolkata",
        incident_description="Multi-car collision injury",
        requested_amount=Decimal("60000.00"),
        registered_by=assessor.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.READY_FOR_ASSESSMENT,
    )
    db_session.add_all([claim1, claim2])
    db_session.commit()

    # Step 1: Propose incident group
    prop_resp = client.post(
        "/api/incident-groups/propose",
        headers=auth_headers(db_session, assessor),
        json={
            "claim_ids": [str(claim1.id), str(claim2.id)],
            "reason": "Both claims originate from the same accident on 2025-03-01",
        },
    )
    assert prop_resp.status_code in (200, 201), prop_resp.text
    group_id = prop_resp.json()["id"]

    # Step 2: Proposer cannot confirm their own group (Four-eyes)
    self_confirm = client.post(
        f"/api/incident-groups/{group_id}/confirm",
        headers=auth_headers(db_session, assessor),
        json={"remarks": "Attempting self confirmation"},
    )
    assert self_confirm.status_code == 403

    # Step 3: Manager confirms
    conf_resp = client.post(
        f"/api/incident-groups/{group_id}/confirm",
        headers=auth_headers(db_session, manager),
        json={"remarks": "Confirmed after manual review of collision report."},
    )
    assert conf_resp.status_code == 200
    assert conf_resp.json()["status"] == "CONFIRMED"

    # Step 4: Separation of duties check 5c: Confirmer (manager) CANNOT decide the claim!
    from app.schemas.decisions import DecisionCreate

    dec_service = DecisionService(db_session)
    db_session.refresh(claim1)
    with pytest.raises(
        DecisionPermissionError,
        match="Separation of duties violation: incident group confirmer cannot decide",
    ):
        dec_service.make_decision(
            claim_id=claim1.id,
            payload=DecisionCreate(
                decision="APPROVED",
                approved_amount=Decimal("40000.00"),
                remarks="Legitimate medical treatment costs confirmed by invoice",
                reason_codes=["LEGITIMATE_CLAIM"],
                expected_status_version=claim1.status_version,
            ),
            actor=manager,
        )

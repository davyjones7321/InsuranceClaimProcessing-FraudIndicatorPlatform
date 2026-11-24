"""Integration test suite for Phase P5: Assessment engine, deterministic replay, and snapshots."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.models import Claim, Coverage, Customer, Policy, PolicyInsuredItem, User
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    LimitScope,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.schemas.assessments import AssessmentComponentCreate, AssessmentCreate
from app.schemas.claims import ClaimCreate
from app.security.password import hash_password
from app.security.session import create_session
from app.services.assessment_service import AssessmentService, AssessmentValidationError
from app.services.claim_service import ClaimService, ClaimValidationError
from app.services.risk_service import RiskService
from app.services.validation_service import ValidationService
from scripts.offline_replay import replay_offline_bundle
from scripts.replay import run_replay


@pytest.fixture
def test_users(db_session: Session) -> dict[str, User]:
    """Create test users for role-based assessment and customer operations."""
    users = {}
    for role in (
        UserRole.CUSTOMER,
        UserRole.STAFF,
        UserRole.ASSESSOR,
        UserRole.MANAGER,
        UserRole.ADMIN,
    ):
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            email=f"p5_{role.value.lower()}_{user_id.hex[:6]}@example.com",
            password_hash=hash_password("StrongPassword123!"),
            role=role,
            is_active=True,
        )
        db_session.add(user)
        users[role.value] = user
    db_session.commit()
    for u in users.values():
        db_session.refresh(u)
    return users


def test_claim_registration_sets_timezone_and_business_date(
    db_session: Session, test_users: dict[str, User]
) -> None:
    """Task 10: Claim registration records UTC registered_at, business date registered_on, and registered_tz."""
    customer_user = test_users[UserRole.CUSTOMER.value]
    cust = Customer(
        id=uuid.uuid4(),
        customer_no=f"CUST-P5-{uuid.uuid4().hex[:6]}",
        full_name="Timezone Test Customer",
        dob=date(1990, 1, 1),
        email=customer_user.email,
        phone="+919876543210",
        address="100 Timezone Street",
        state_region="West Bengal",
    )
    db_session.add(cust)
    customer_user.customer_id = cust.id
    db_session.flush()

    policy = Policy(
        id=uuid.uuid4(),
        policy_no=f"POL-P5-TZ-{uuid.uuid4().hex[:6]}",
        customer_id=cust.id,
        product_type=ProductType.HEALTH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("10000.00"),
        sum_insured=Decimal("500000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(policy)

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="HOSPITALIZATION",
        limit_scope=LimitScope.PER_POLICY_PERIOD,
        limit_amount=Decimal("200000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("2000.00"),
        copay_percent=Decimal("10.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)
    db_session.commit()

    service = ClaimService(db_session)
    payload = ClaimCreate(
        policy_id=policy.id,
        claim_category="HOSPITALIZATION",
        incident_date=date(2026, 5, 10),
        incident_location="Kolkata Central Hospital",
        incident_description="Inpatient hospitalization for acute appendicitis",
        requested_amount=Decimal("50000.00"),
        incident_group_id=uuid.uuid4(),
    )
    claim = service.register_claim(payload, actor=customer_user)

    assert claim.registered_tz == "Asia/Kolkata"
    assert claim.registered_on is not None
    assert isinstance(claim.registered_on, date)
    assert claim.incident_group_id == payload.incident_group_id


def test_utc_local_midnight_boundary_business_date() -> None:
    """Task 10: Test UTC to local business date conversion across midnight boundary."""
    # 2026-05-01 20:00:00 UTC is 2026-05-02 01:30:00 in Asia/Kolkata (+05:30)
    utc_dt = datetime(2026, 5, 1, 20, 0, 0, tzinfo=ZoneInfo("UTC"))
    tz = ZoneInfo("Asia/Kolkata")
    business_date = utc_dt.astimezone(tz).date()

    assert business_date == date(2026, 5, 2)


def test_insured_item_verification_vehicle_and_property(
    db_session: Session, test_users: dict[str, User]
) -> None:
    """Task 7: Claimed vehicle or property must match policy insured items."""
    staff_user = test_users[UserRole.STAFF.value]
    cust = Customer(
        id=uuid.uuid4(),
        customer_no=f"CUST-P5-ITEM-{uuid.uuid4().hex[:6]}",
        full_name="Insured Item Customer",
        dob=date(1992, 5, 15),
        email="item_cust@example.com",
        phone="+919876543211",
        address="200 Motor Road",
        state_region="Maharashtra",
    )
    db_session.add(cust)

    policy = Policy(
        id=uuid.uuid4(),
        policy_no=f"POL-P5-VEH-{uuid.uuid4().hex[:6]}",
        customer_id=cust.id,
        product_type=ProductType.VEHICLE,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("15000.00"),
        sum_insured=Decimal("800000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(policy)

    vehicle_item = PolicyInsuredItem(
        id=uuid.uuid4(),
        policy_id=policy.id,
        item_type="VEHICLE",
        identifier="MH-02-AB-1234",
    )
    db_session.add(vehicle_item)

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="COLLISION",
        limit_scope=LimitScope.PER_CLAIM,
        limit_amount=Decimal("500000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("2500.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)
    db_session.commit()

    service = ClaimService(db_session)

    # 1. Claim without vehicle item fails because policy has vehicle items declared
    bad_payload_no_item = ClaimCreate(
        policy_id=policy.id,
        customer_id=cust.id,
        claim_category="COLLISION",
        incident_date=date(2026, 6, 1),
        incident_location="Mumbai Highway",
        incident_description="Front bumper damage",
        requested_amount=Decimal("40000.00"),
        insured_item_id=None,
    )
    with pytest.raises(ClaimValidationError, match="requires specifying an insured vehicle item"):
        service.register_claim(bad_payload_no_item, actor=staff_user)

    # 2. Claim with valid vehicle item succeeds
    good_payload = ClaimCreate(
        policy_id=policy.id,
        customer_id=cust.id,
        claim_category="COLLISION",
        incident_date=date(2026, 6, 1),
        incident_location="Mumbai Highway",
        incident_description="Front bumper damage",
        requested_amount=Decimal("40000.00"),
        insured_item_id=vehicle_item.id,
    )
    claim = service.register_claim(good_payload, actor=staff_user)
    assert claim.insured_item_id == vehicle_item.id


def test_coverage_edits_blocked_when_referenced_by_claim(
    db_session: Session, test_users: dict[str, User], client: TestClient
) -> None:
    """Task 5: Coverage edits are blocked with 409 Conflict once a claim references the coverage."""
    admin_user = test_users[UserRole.ADMIN.value]
    cust = Customer(
        id=uuid.uuid4(),
        customer_no=f"CUST-P5-COV-{uuid.uuid4().hex[:6]}",
        full_name="Coverage Lock Customer",
        dob=date(1985, 3, 20),
        email="cov_lock@example.com",
        phone="+919876543212",
        address="300 Policy Lane",
        state_region="Delhi",
    )
    db_session.add(cust)

    policy = Policy(
        id=uuid.uuid4(),
        policy_no=f"POL-P5-COV-{uuid.uuid4().hex[:6]}",
        customer_id=cust.id,
        product_type=ProductType.HEALTH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("12000.00"),
        sum_insured=Decimal("300000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(policy)

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="CONSULTATION",
        limit_scope=LimitScope.PER_POLICY_PERIOD,
        limit_amount=Decimal("50000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("500.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-P5-{uuid.uuid4().hex[:6]}",
        policy_id=policy.id,
        customer_id=cust.id,
        claim_category="CONSULTATION",
        incident_date=date(2026, 5, 1),
        incident_location="Delhi Medical Center",
        incident_description="Specialist consultation",
        requested_amount=Decimal("3000.00"),
        registered_at=utc_now(),
        registered_on=date(2026, 5, 2),
        registered_tz="Asia/Kolkata",
        registered_by=admin_user.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.REGISTERED,
    )
    db_session.add(claim)
    db_session.commit()

    # Attempt to edit coverage via API client with admin credentials
    session_id = create_session(db_session, admin_user)
    db_session.commit()
    admin_hdr = {"Authorization": f"Bearer {session_id}"}

    update_resp = client.put(
        f"/api/coverages/{coverage.id}",
        headers=admin_hdr,
        json={"deductible": "1000.00"},
    )
    assert update_resp.status_code == 409
    assert "cannot be edited because 1 claim(s) reference it" in update_resp.json()["detail"]


def test_assessment_flow_components_and_deterministic_replay(
    db_session: Session, test_users: dict[str, User]
) -> None:
    """Task 1, 2, 8: Assessment creation, cap calculation, snapshot persistence, and replay from stored snapshot."""
    assessor_user = test_users[UserRole.ASSESSOR.value]
    staff_user = test_users[UserRole.STAFF.value]
    cust = Customer(
        id=uuid.uuid4(),
        customer_no=f"CUST-P5-ASSESS-{uuid.uuid4().hex[:6]}",
        full_name="Assessment Test Customer",
        dob=date(1991, 7, 10),
        email="assess_cust@example.com",
        phone="+919876543213",
        address="400 Hospital Road",
        state_region="Karnataka",
    )
    db_session.add(cust)

    policy = Policy(
        id=uuid.uuid4(),
        policy_no=f"POL-P5-HEALTH-{uuid.uuid4().hex[:6]}",
        customer_id=cust.id,
        product_type=ProductType.HEALTH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("20000.00"),
        sum_insured=Decimal("500000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(policy)

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="SURGERY",
        limit_scope=LimitScope.PER_POLICY_PERIOD,
        limit_amount=Decimal("150000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("5000.00"),
        copay_percent=Decimal("10.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-P5-SURG-{uuid.uuid4().hex[:6]}",
        policy_id=policy.id,
        customer_id=cust.id,
        claim_category="SURGERY",
        incident_date=date(2026, 6, 1),
        incident_location="Bangalore Surgical Clinic",
        incident_description="Orthopedic knee reconstruction surgery",
        requested_amount=Decimal("100000.00"),
        registered_at=utc_now(),
        registered_on=date(2026, 6, 2),
        registered_tz="Asia/Kolkata",
        registered_by=staff_user.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.REGISTERED,
    )
    db_session.add(claim)
    db_session.commit()

    # 1. Validation and Risk run to populate stored snapshots
    val_service = ValidationService(db_session)
    val_run = val_service.validate_claim(claim.id, actor=staff_user)
    assert val_run.inputs_snapshot is not None
    assert "claim" in val_run.inputs_snapshot

    # Set status to READY_FOR_ASSESSMENT to simulate progression to risk evaluation
    claim.status = ClaimStatus.READY_FOR_ASSESSMENT
    db_session.commit()

    risk_service = RiskService(db_session)
    risk_eval = risk_service.evaluate_claim_risk(claim.id, actor=assessor_user)
    assert risk_eval.inputs_snapshot is not None
    assert "claim" in risk_eval.inputs_snapshot

    claim.status = ClaimStatus.READY_FOR_ASSESSMENT
    db_session.commit()

    # 2. Assessment creation with typed components
    assess_service = AssessmentService(db_session)

    # Assessed loss = 100,000; deductible = 5,000 -> 95,000; copay 10% -> 85,500
    # Eligible amount exceeding cap fails
    bad_payload = AssessmentCreate(
        assessed_loss=Decimal("100000.00"),
        eligible_amount=Decimal("90000.00"),  # > 85,500
        remarks="Attempting to exceed engine cap",
        components=[
            AssessmentComponentCreate(
                component_type="SURGERY",
                claimed_amount=Decimal("70000.00"),
                assessed_amount=Decimal("70000.00"),
            ),
            AssessmentComponentCreate(
                component_type="ROOM_RENT",
                claimed_amount=Decimal("30000.00"),
                assessed_amount=Decimal("30000.00"),
            ),
        ],
    )
    with pytest.raises(AssessmentValidationError, match="exceeds maximum eligible cap 85500.00"):
        assess_service.create_assessment(claim.id, payload=bad_payload, actor=assessor_user)

    # Valid assessment matching cap
    good_payload = AssessmentCreate(
        assessed_loss=Decimal("100000.00"),
        eligible_amount=Decimal("85500.00"),
        remarks="Surgery and room rent verified against hospital documentation",
        components=[
            AssessmentComponentCreate(
                component_type="SURGERY",
                claimed_amount=Decimal("70000.00"),
                assessed_amount=Decimal("70000.00"),
            ),
            AssessmentComponentCreate(
                component_type="ROOM_RENT",
                claimed_amount=Decimal("30000.00"),
                assessed_amount=Decimal("30000.00"),
            ),
        ],
    )
    assessment = assess_service.create_assessment(
        claim.id, payload=good_payload, actor=assessor_user
    )
    assert assessment.max_eligible_amount == Decimal("85500.00")
    assert assessment.eligible_amount == Decimal("85500.00")
    assert assessment.inputs_snapshot is not None
    assert assessment.inputs_snapshot["assessed_loss"] == "100000.00"

    # 3. Deterministic replay from stored snapshots (Fix F7)
    replay_exit_code = run_replay(claim_id=claim.id, db=db_session, verbose=True)
    assert replay_exit_code == 0


def test_offline_bundle_replay_runner() -> None:
    """Task 11: Offline replay runner executes without DB credentials."""
    bundle = {
        "ruleset": {"ruleset_version": "v1", "engine_version": "1.0.0"},
        "assessments": [
            {
                "id": "assess-01",
                "inputs_snapshot": {
                    "product_type": "HEALTH",
                    "requested_amount": "100000.00",
                    "remaining_limit": "200000.00",
                    "assessed_loss": "100000.00",
                    "deductible": "5000.00",
                    "copay_percent": "10.00",
                    "depreciation": "0.00",
                    "sum_insured": "0.00",
                    "value_at_risk": "0.00",
                    "components": [],
                    "ruleset_version": "v1",
                    "engine_version": "1.0.0",
                },
                "expected_output": {
                    "max_eligible_amount": "85500.00",
                },
            }
        ],
    }

    report = replay_offline_bundle(bundle, verbose=True)
    assert report["status"] == "PASSED"
    assert report["assessments_checked"] == 1
    assert len(report["failures"]) == 0

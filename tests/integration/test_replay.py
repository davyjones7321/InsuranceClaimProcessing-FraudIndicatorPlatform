"""Integration test verifying that scripts/replay.py successfully replays validation, risk, and assessment runs."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import (
    Assessment,
    Claim,
    Coverage,
    Customer,
    Policy,
    User,
)
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.engine.assessment_cap import calculate_assessment_cap
from app.engine.types import AssessmentInput
from app.security.password import hash_password
from scripts.replay import run_replay


def test_replay_script_reproduces_all_evaluations(db_session: Session) -> None:
    """Run deterministic replay tool against seeded evaluation records and verify exact match."""
    customer = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-REPLAY-001",
        full_name="Natasha Romanoff",
        dob=date(1984, 11, 22),
        email="natasha.rep@example.com",
        phone="+15557766",
        address="100 Spy St",
        state_region="NY",
    )
    user_staff = User(
        id=uuid.uuid4(),
        email="staff.rep@example.com",
        password_hash=hash_password("StaffPassSecure123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_assessor = User(
        id=uuid.uuid4(),
        email="assessor.rep@example.com",
        password_hash=hash_password("AssessorPassSecure123!"),
        role=UserRole.ASSESSOR,
        is_active=True,
    )
    db_session.add_all([customer, user_staff, user_assessor])
    db_session.flush()

    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-REPLAY-001",
        customer_id=customer.id,
        product_type=ProductType.HEALTH,
        start_date=date.today() - timedelta(days=50),
        end_date=date.today() + timedelta(days=315),
        premium=Decimal("30000.00"),
        sum_insured=Decimal("500000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(policy)
    db_session.flush()

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="INPATIENT",
        limit_amount=Decimal("300000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("2000.00"),
        copay_percent=Decimal("10.00"),
        waiting_period_days=0,
        is_excluded=False,
    )
    db_session.add(coverage)
    db_session.flush()

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-000601",
        policy_id=policy.id,
        customer_id=customer.id,
        claim_category="INPATIENT",
        incident_date=date.today() - timedelta(days=10),
        incident_location="New York Hospital",
        incident_description="Inpatient hospitalization surgery.",
        requested_amount=Decimal("40000.00"),
        registered_by=user_staff.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.ASSESSED,
        status_version=4,
    )
    db_session.add(claim)
    db_session.flush()

    # Create Assessment
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=claim.requested_amount,
        remaining_limit=Decimal("300000.00"),
        assessed_loss=Decimal("40000.00"),
        deductible=Decimal("2000.00"),
        copay_percent=Decimal("10.00"),
    )
    cap_out = calculate_assessment_cap(inp)

    assessment = Assessment(
        id=uuid.uuid4(),
        claim_id=claim.id,
        assessor_id=user_assessor.id,
        assessed_loss=Decimal("40000.00"),
        max_eligible_amount=cap_out.max_eligible_amount,
        eligible_amount=cap_out.max_eligible_amount,
        calculation_trace=cap_out.calculation_trace,
        remarks="Accurate medical invoices provided.",
    )
    db_session.add(assessment)
    db_session.commit()

    # Run replay across database
    exit_code = run_replay(db=db_session, verbose=True)
    assert exit_code == 0

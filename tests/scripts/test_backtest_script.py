"""Tests for scripts/backtest.py (Task 12)."""

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import (
    Claim,
    Customer,
    Investigation,
    Policy,
    RiskEvaluation,
    RiskIndicatorResult,
    User,
)
from app.domain.enums import (
    ClaimStatus,
    InvestigationOutcome,
    InvestigationStatus,
    PolicyStatus,
    ProductType,
    RiskCategory,
    RiskCompleteness,
    UserRole,
)
from scripts.backtest import (
    FIXED_DISCLAIMER,
    SELECTION_BIAS_CAVEAT,
    format_text_report,
    run_backtest,
)


def test_forbidden_words_in_backtest_script() -> None:
    """Task 12: A test strictly forbids the words 'accuracy' and 'detection rate'."""
    script_path = Path("scripts/backtest.py")
    assert script_path.exists(), "scripts/backtest.py must exist"

    content = script_path.read_text(encoding="utf-8")
    lower_content = content.lower()

    assert "accuracy" not in lower_content, "Forbidden word 'accuracy' found in scripts/backtest.py"
    assert "detection rate" not in lower_content, (
        "Forbidden phrase 'detection rate' found in scripts/backtest.py"
    )


def test_backtest_report_execution_and_content(db_session: Session) -> None:
    """Verify backtest execution, metrics calculation, and mandatory caveats."""
    # Create customer, policy, user
    customer = Customer(
        id=uuid.uuid4(),
        customer_no=f"CUST-BT-{uuid.uuid4().hex[:6]}",
        full_name="Anita Roy",
        dob=date(1990, 1, 1),
        email=f"anita_{uuid.uuid4().hex[:6]}@example.com",
        phone="+919876543210",
        address="12 Park Street, Kolkata",
        state_region="West Bengal",
    )
    db_session.add(customer)

    policy = Policy(
        id=uuid.uuid4(),
        policy_no=f"POL-BT-{uuid.uuid4().hex[:6]}",
        customer_id=customer.id,
        product_type=ProductType.HEALTH,
        status=PolicyStatus.ACTIVE,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 1),
        premium=Decimal("15000.00"),
        sum_insured=Decimal("500000.00"),
    )
    db_session.add(policy)

    investigator = User(
        id=uuid.uuid4(),
        email=f"inv_{uuid.uuid4().hex[:6]}@example.com",
        role=UserRole.INVESTIGATOR,
        password_hash="mock",
    )
    db_session.add(investigator)
    db_session.flush()

    # Claim 1: Flagged with score 55 and investigation CONCERNS_CONFIRMED
    claim1 = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-BT1-{uuid.uuid4().hex[:4]}",
        customer_id=customer.id,
        policy_id=policy.id,
        incident_date=date(2025, 6, 1),
        claim_category="INPATIENT",
        incident_location="Kolkata",
        incident_description="Medical treatment",
        registered_by=investigator.id,
        channel="STAFF",
        status=ClaimStatus.ASSESSED,
        requested_amount=Decimal("50000.00"),
    )
    db_session.add(claim1)
    db_session.flush()

    eval1 = RiskEvaluation(
        id=uuid.uuid4(),
        claim_id=claim1.id,
        ruleset_version="1.0.0",
        engine_version="1.0.0",
        evaluated_on=date(2025, 6, 2),
        completeness=RiskCompleteness.COMPLETE,
        category=RiskCategory.HIGH,
        score=55,
        composed_label="COMPLETE HIGH",
        requires_investigation=True,
        routing_reasons=["Score 55 meets or exceeds investigation threshold 40"],
        pilot_banner="PILOT",
        group_scores={"FREQUENCY": 25, "TIMING": 30},
        inputs_snapshot={},
    )
    db_session.add(eval1)
    db_session.flush()

    ind1 = RiskIndicatorResult(
        id=uuid.uuid4(),
        evaluation_id=eval1.id,
        indicator_id="R01",
        indicator_version="1.0.0",
        group="FREQUENCY",
        weight=25,
        triggered=True,
        weight_applied=25,
        sufficient_alone=True,
        evidence_level="NONE",
        message="Frequency trigger",
        status="TRIGGERED",
        evidence={},
    )
    db_session.add(ind1)

    inv1 = Investigation(
        id=uuid.uuid4(),
        claim_id=claim1.id,
        assigned_to=investigator.id,
        assigned_by=investigator.id,
        status=InvestigationStatus.COMPLETED,
        outcome=InvestigationOutcome.CONCERNS_CONFIRMED,
        referral_type="ENGINE",
    )
    db_session.add(inv1)

    # Claim 2: Unflagged with score 10 and manual referral CLEARED
    claim2 = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-BT2-{uuid.uuid4().hex[:4]}",
        customer_id=customer.id,
        policy_id=policy.id,
        incident_date=date(2025, 7, 1),
        claim_category="INPATIENT",
        incident_location="Kolkata",
        incident_description="Medical treatment",
        registered_by=investigator.id,
        channel="STAFF",
        status=ClaimStatus.ASSESSED,
        requested_amount=Decimal("15000.00"),
    )
    db_session.add(claim2)
    db_session.flush()

    eval2 = RiskEvaluation(
        id=uuid.uuid4(),
        claim_id=claim2.id,
        ruleset_version="1.0.0",
        engine_version="1.0.0",
        evaluated_on=date(2025, 7, 2),
        completeness=RiskCompleteness.COMPLETE,
        category=RiskCategory.LOW,
        score=10,
        composed_label="COMPLETE LOW",
        requires_investigation=False,
        routing_reasons=[],
        pilot_banner="PILOT",
        group_scores={"TIMING": 10},
        inputs_snapshot={},
    )
    db_session.add(eval2)

    inv2 = Investigation(
        id=uuid.uuid4(),
        claim_id=claim2.id,
        assigned_to=investigator.id,
        assigned_by=investigator.id,
        status=InvestigationStatus.COMPLETED,
        outcome=InvestigationOutcome.CLEARED,
        referral_type="MANUAL",
    )
    db_session.add(inv2)
    db_session.commit()

    report = run_backtest(db=db_session)
    assert report.total_evaluations >= 2
    assert report.total_flagged >= 1
    assert report.overall_flag_rate > 0.0

    text_output = format_text_report(report)
    lower_output = text_output.lower()

    # Verify forbidden words do NOT appear in the report output
    assert "accuracy" not in lower_output, (
        "Forbidden word 'accuracy' found in backtest report output"
    )
    assert "detection rate" not in lower_output, (
        "Forbidden phrase 'detection rate' found in backtest report output"
    )

    # Verify fixed disclaimer and selection bias caveat are present
    assert FIXED_DISCLAIMER in text_output
    assert SELECTION_BIAS_CAVEAT in text_output

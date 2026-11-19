"""Security invariant test proving no code path converts a risk score into a claim rejection."""

import ast
from pathlib import Path

from app.engine.risk import evaluate_risk
from app.engine.types import (
    ClaimInput,
    CoverageInput,
    HistoricalClaimInput,
    PolicyInput,
    RiskCategoryEnum,
    RiskInput,
)
from app.rules.loader import ACTIVE_RULESET

APP_DIR = Path(__file__).resolve().parent.parent.parent / "app"


def test_pure_risk_engine_never_outputs_rejection() -> None:
    """Verify that pure evaluate_risk returns score/category/requires_investigation, never a rejection."""
    from datetime import date
    from decimal import Decimal

    claim = ClaimInput(
        claim_id="clm-999",
        claim_ref="CLM-999",
        customer_id="cust-1",
        policy_id="pol-1",
        claim_category="CAT_1",
        incident_date=date(2026, 3, 1),
        incident_location="Hospital",
        incident_description="Test",
        requested_amount=Decimal("100000.00"),
        registered_date=date(2026, 3, 20),
    )
    pol = PolicyInput(
        policy_id="pol-1",
        policy_no="POL-1",
        customer_id="cust-1",
        product_type="HEALTH",
        start_date=date(2026, 3, 1),  # Early claim (R05)
        end_date=date(2026, 3, 2),  # Late in policy (R06)
        status="ACTIVE",
    )
    cov = CoverageInput(
        coverage_id="cov-1",
        claim_category="CAT_1",
        limit_amount=Decimal("50000.00"),
        consumed_amount=Decimal("49000.00"),  # High amount vs limit (R03)
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
    )
    # Generate 5 prior claims (R01, R02)
    history = tuple(
        HistoricalClaimInput(
            claim_id=f"hc-{i}",
            claim_ref=f"CLM-HIST-{i}",
            customer_id="cust-1",
            policy_id="pol-1",
            claim_category="CAT_1",
            incident_date=date(2026, 3, 1),
            registered_date=date(2026, 3, 1),
            requested_amount=Decimal("100000.00"),
            status="VALIDATED",
        )
        for i in range(5)
    )

    risk_in = RiskInput(
        claim=claim,
        customer_name="Alice",
        customer_state_region="NY",
        policy=pol,
        coverage=cov,
        documents=(),
        historical_claims=history,
        peer_approved_amounts=(),
        evaluation_date=date(2026, 3, 20),
        ruleset=ACTIVE_RULESET,
    )

    out = evaluate_risk(risk_in)
    # Score must be high
    assert out.score >= 50
    assert out.category == RiskCategoryEnum.HIGH
    assert out.requires_investigation is True

    # Output attributes must not contain any status or rejection fields
    assert not hasattr(out, "status")
    assert not hasattr(out, "rejection_reason")
    assert not hasattr(out, "is_rejected")


def test_ast_risk_service_never_transitions_to_rejected() -> None:
    """Scan AST of RiskService to guarantee it never references or transitions to ClaimStatus.REJECTED."""
    risk_service_path = APP_DIR / "services" / "risk_service.py"
    assert risk_service_path.is_file()

    code = risk_service_path.read_text(encoding="utf-8")
    tree = ast.parse(code, filename=str(risk_service_path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            # Assert ClaimStatus.REJECTED is never referenced
            if node.attr == "REJECTED":
                raise AssertionError(
                    f"Forbidden reference to ClaimStatus.REJECTED at line {node.lineno} in risk_service.py"
                )

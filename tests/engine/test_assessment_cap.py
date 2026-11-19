"""Unit and property tests for pure assessment cap calculation engine."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.engine.assessment_cap import calculate_assessment_cap
from app.engine.types import AssessmentInput


def test_health_assessment_cap_calculation() -> None:
    """Verify HEALTH calculation: net = max(0, loss - deductible); payable = min(limit, net * (1 - copay/100))."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("15000.00"),
        remaining_limit=Decimal("50000.00"),
        assessed_loss=Decimal("10000.00"),
        deductible=Decimal("1000.00"),
        copay_percent=Decimal("10.00"),
    )
    out = calculate_assessment_cap(inp)
    # net = 10,000 - 1,000 = 9,000; after copay 10% = 9,000 * 0.9 = 8,100
    assert out.max_eligible_amount == Decimal("8100.00")
    assert out.calculation_trace["net_after_deductible"] == "9000.00"
    assert out.calculation_trace["after_copay"] == "8100.00"
    assert out.calculation_trace["max_eligible_amount"] == "8100.00"


def test_health_capped_by_remaining_limit() -> None:
    """Verify HEALTH cap is constrained by remaining limit."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("100000.00"),
        remaining_limit=Decimal("50000.00"),
        assessed_loss=Decimal("100000.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("20.00"),
    )
    out = calculate_assessment_cap(inp)
    # after copay = 80,000, remaining limit = 50,000 -> 50,000
    assert out.max_eligible_amount == Decimal("50000.00")
    assert out.calculation_trace["payable"] == "50000.00"


def test_vehicle_assessment_cap_calculation() -> None:
    """Verify VEHICLE calculation: after_depr = max(0, loss - depr); payable = min(limit, max(0, after_depr - deductible))."""
    inp = AssessmentInput(
        product_type="VEHICLE",
        requested_amount=Decimal("60000.00"),
        remaining_limit=Decimal("100000.00"),
        assessed_loss=Decimal("50000.00"),
        depreciation=Decimal("5000.00"),
        deductible=Decimal("2500.00"),
    )
    out = calculate_assessment_cap(inp)
    # after_depr = 45,000; after_deductible = 42,500; limit = 100,000 -> 42,500
    assert out.max_eligible_amount == Decimal("42500.00")
    assert out.calculation_trace["after_depreciation"] == "45000.00"
    assert out.calculation_trace["after_deductible"] == "42500.00"


def test_property_assessment_cap_with_underinsurance() -> None:
    """Verify PROPERTY coinsurance factor: adjusted = loss * min(1, sum_insured / value_at_risk)."""
    inp = AssessmentInput(
        product_type="PROPERTY",
        requested_amount=Decimal("120000.00"),
        remaining_limit=Decimal("800000.00"),
        assessed_loss=Decimal("100000.00"),
        deductible=Decimal("10000.00"),
        sum_insured=Decimal("800000.00"),
        value_at_risk=Decimal("1000000.00"),
    )
    out = calculate_assessment_cap(inp)
    # factor = 800,000 / 1,000,000 = 0.8; adjusted = 80,000; after deductible = 70,000
    assert out.max_eligible_amount == Decimal("70000.00")
    assert out.calculation_trace["coinsurance_factor"] == "0.8000"
    assert out.calculation_trace["adjusted_loss"] == "80000.00"
    assert out.calculation_trace["after_deductible"] == "70000.00"


def test_property_overinsurance_capped_at_one() -> None:
    """Coinsurance factor must never exceed 1.0 even if sum_insured > value_at_risk."""
    inp = AssessmentInput(
        product_type="PROPERTY",
        requested_amount=Decimal("50000.00"),
        remaining_limit=Decimal("2000000.00"),
        assessed_loss=Decimal("50000.00"),
        deductible=Decimal("5000.00"),
        sum_insured=Decimal("1500000.00"),
        value_at_risk=Decimal("1000000.00"),
    )
    out = calculate_assessment_cap(inp)
    # factor = 1.0; adjusted = 50,000; after deductible = 45,000
    assert out.max_eligible_amount == Decimal("45000.00")
    assert out.calculation_trace["coinsurance_factor"] == "1.0000"


def test_loss_less_than_deductible_yields_zero() -> None:
    """When assessed loss is less than deductible, max eligible amount must be 0.00."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("500.00"),
        remaining_limit=Decimal("10000.00"),
        assessed_loss=Decimal("500.00"),
        deductible=Decimal("1000.00"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("0.00")


def test_negative_inputs_raise_value_error() -> None:
    """Engine must strictly reject negative monetary inputs."""
    base = {
        "product_type": "HEALTH",
        "requested_amount": Decimal("100.00"),
        "remaining_limit": Decimal("1000.00"),
        "assessed_loss": Decimal("50.00"),
    }

    with pytest.raises(ValueError, match="Assessed loss cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**base, "assessed_loss": Decimal("-50.00")}))

    with pytest.raises(ValueError, match="Remaining limit cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**base, "remaining_limit": Decimal("-10.00")}))

    with pytest.raises(ValueError, match="Deductible cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**base, "deductible": Decimal("-10.00")}))

    with pytest.raises(ValueError, match="Copay percent must be between 0 and 100"):
        calculate_assessment_cap(AssessmentInput(**{**base, "copay_percent": Decimal("-5.00")}))

    with pytest.raises(ValueError, match="Copay percent must be between 0 and 100"):
        calculate_assessment_cap(AssessmentInput(**{**base, "copay_percent": Decimal("105.00")}))

    with pytest.raises(ValueError, match="Depreciation cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**base, "depreciation": Decimal("-5.00")}))

    with pytest.raises(ValueError, match="Sum insured cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**base, "sum_insured": Decimal("-5.00")}))

    with pytest.raises(ValueError, match="Value at risk cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**base, "value_at_risk": Decimal("-5.00")}))


def test_fallback_product_type() -> None:
    """Fallback product type applies basic deductible and limit cap."""
    inp = AssessmentInput(
        product_type="TRAVEL",
        requested_amount=Decimal("2000.00"),
        remaining_limit=Decimal("5000.00"),
        assessed_loss=Decimal("2000.00"),
        deductible=Decimal("200.00"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("1800.00")
    assert out.calculation_trace["product_type"] == "TRAVEL"


def test_property_zero_value_at_risk() -> None:
    """When value at risk is zero, coinsurance factor defaults to 1.0."""
    inp = AssessmentInput(
        product_type="PROPERTY",
        requested_amount=Decimal("10000.00"),
        remaining_limit=Decimal("50000.00"),
        assessed_loss=Decimal("10000.00"),
        deductible=Decimal("500.00"),
        sum_insured=Decimal("100000.00"),
        value_at_risk=Decimal("0.00"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("9500.00")
    assert out.calculation_trace["coinsurance_factor"] == "1.0000"


@given(
    loss=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("1000000.00"), places=2),
    limit=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("1000000.00"), places=2),
    deductible=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("50000.00"), places=2),
    copay=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("100.00"), places=2),
)
def test_hypothesis_health_cap_invariants(
    loss: Decimal, limit: Decimal, deductible: Decimal, copay: Decimal
) -> None:
    """Property test: max_eligible_amount is always between 0 and min(limit, loss)."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=loss,
        remaining_limit=limit,
        assessed_loss=loss,
        deductible=deductible,
        copay_percent=copay,
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount >= Decimal("0.00")
    assert out.max_eligible_amount <= limit
    assert out.max_eligible_amount <= loss

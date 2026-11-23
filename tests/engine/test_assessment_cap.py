"""Unit and property tests for pure assessment cap calculation engine (P5 Tasks 1-6)."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.engine.assessment_cap import calculate_assessment_cap
from app.engine.product_profiles import FORMULA_SOURCE, UnsupportedProductError
from app.engine.types import AssessmentComponentInput, AssessmentInput


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
    assert out.calculation_trace["formula_source"] == FORMULA_SOURCE
    assert out.calculation_trace["net_after_deductible"] == "9000.00"
    assert out.calculation_trace["after_copay"] == "8100.00"
    assert out.calculation_trace["max_eligible_amount"] == "8100.00"


def test_appendix_c_health_golden() -> None:
    """Appendix C: HEALTH assessed loss 100,000; deductible 5,000; copay 10%; remaining 200,000 -> payable 85,500.00."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("100000.00"),
        remaining_limit=Decimal("200000.00"),
        assessed_loss=Decimal("100000.00"),
        deductible=Decimal("5000.00"),
        copay_percent=Decimal("10.00"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("85500.00")


def test_appendix_c_health_single_final_rounding() -> None:
    """Appendix C: loss 10,001.55, no deductible, copay 12.5%: 10,001.55 * 0.875 = 8,751.35625 -> rounds to 8,751.36."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("10001.55"),
        remaining_limit=Decimal("50000.00"),
        assessed_loss=Decimal("10001.55"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("12.50"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("8751.36")


def test_appendix_c_vehicle_with_components_golden() -> None:
    """Appendix C: parts 60,000 with 20% depreciation (12,000) gives 48,000; labour 20,000; total 68,000; deductible 2,500 -> 65,500.00."""
    inp = AssessmentInput(
        product_type="VEHICLE",
        requested_amount=Decimal("80000.00"),
        remaining_limit=Decimal("100000.00"),
        assessed_loss=Decimal("80000.00"),
        depreciation=Decimal("12000.00"),  # 20% of 60,000 parts
        deductible=Decimal("2500.00"),
        components=(
            AssessmentComponentInput(
                component_type="PARTS",
                claimed_amount=Decimal("60000.00"),
                assessed_amount=Decimal("60000.00"),
            ),
            AssessmentComponentInput(
                component_type="LABOUR",
                claimed_amount=Decimal("20000.00"),
                assessed_amount=Decimal("20000.00"),
            ),
        ),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("65500.00")
    assert out.calculation_trace["after_depreciation"] == "68000.00"
    assert out.calculation_trace["after_deductible"] == "65500.00"


def test_appendix_c_property_golden() -> None:
    """Appendix C: loss 800,000; sum insured 5,000,000; value at risk 8,000,000; factor 0.625 gives 500,000; deductible 25,000 -> 475,000.00."""
    inp = AssessmentInput(
        product_type="PROPERTY",
        requested_amount=Decimal("800000.00"),
        remaining_limit=Decimal("5000000.00"),
        assessed_loss=Decimal("800000.00"),
        deductible=Decimal("25000.00"),
        sum_insured=Decimal("5000000.00"),
        value_at_risk=Decimal("8000000.00"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("475000.00")
    assert out.calculation_trace["coinsurance_factor"] == "0.6250"
    assert out.calculation_trace["adjusted_loss"] == "500000.00"


def test_appendix_c_limit_cap_applied() -> None:
    """Appendix C: HEALTH adjusted amount 85,500 with remaining limit 60,000 gives payable 60,000, limit_capped = true, shortfall 25,500."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("100000.00"),
        remaining_limit=Decimal("60000.00"),
        assessed_loss=Decimal("100000.00"),
        deductible=Decimal("5000.00"),
        copay_percent=Decimal("10.00"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("60000.00")
    trace = out.calculation_trace
    cap_trace = trace["step_3_limit_cap"]
    assert isinstance(cap_trace, dict)
    assert cap_trace["limit_capped"] is True
    assert cap_trace["shortfall"] == "25500.00"
    assert cap_trace["cap_reason"] == "LIMIT_APPLIED"


def test_unsupported_product_strictly_rejected() -> None:
    """Task 2 & Gate: Unsupported product types (e.g. TRAVEL) must be explicitly rejected with no generic fallback."""
    inp = AssessmentInput(
        product_type="TRAVEL",
        requested_amount=Decimal("2000.00"),
        remaining_limit=Decimal("5000.00"),
        assessed_loss=Decimal("2000.00"),
        deductible=Decimal("200.00"),
    )
    with pytest.raises(UnsupportedProductError, match="Unsupported product type 'TRAVEL'"):
        calculate_assessment_cap(inp)


def test_float_rejection() -> None:
    """Task 3: Floats must be rejected to prevent precision errors."""
    with pytest.raises(TypeError, match="Float value"):
        calculate_assessment_cap(
            AssessmentInput(
                product_type="HEALTH",
                requested_amount=100.0,  # type: ignore[arg-type]
                remaining_limit=Decimal("1000.00"),
                assessed_loss=Decimal("100.00"),
            )
        )


def test_assessed_loss_exceeds_requested_amount_rejected() -> None:
    """Task 1: Assessor loss cannot exceed requested amount."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("5000.00"),
        remaining_limit=Decimal("10000.00"),
        assessed_loss=Decimal("6000.00"),
    )
    with pytest.raises(ValueError, match="cannot exceed requested amount"):
        calculate_assessment_cap(inp)


def test_component_zeroing_without_reason_rejected() -> None:
    """Task 1: If a component is zeroed out by assessor, reason is mandatory."""
    inp = AssessmentInput(
        product_type="VEHICLE",
        requested_amount=Decimal("10000.00"),
        remaining_limit=Decimal("20000.00"),
        assessed_loss=Decimal("0.00"),
        components=(
            AssessmentComponentInput(
                component_type="PARTS",
                claimed_amount=Decimal("10000.00"),
                assessed_amount=Decimal("0.00"),
                zeroed_reason="",  # empty reason
            ),
        ),
    )
    with pytest.raises(ValueError, match="no zeroed_reason was provided"):
        calculate_assessment_cap(inp)


def test_component_zeroing_with_reason_accepted() -> None:
    """Task 1: Assessor can zero component when valid reason is provided."""
    inp = AssessmentInput(
        product_type="VEHICLE",
        requested_amount=Decimal("10000.00"),
        remaining_limit=Decimal("20000.00"),
        assessed_loss=Decimal("0.00"),
        components=(
            AssessmentComponentInput(
                component_type="PARTS",
                claimed_amount=Decimal("10000.00"),
                assessed_amount=Decimal("0.00"),
                zeroed_reason="Pre-existing damage not covered by policy incident",
            ),
        ),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("0.00")


def test_boundary_loss_at_or_below_deductible_gives_zero() -> None:
    """Task 6: Loss <= deductible gives 0.00."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("1000.00"),
        remaining_limit=Decimal("10000.00"),
        assessed_loss=Decimal("1000.00"),
        deductible=Decimal("1000.00"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("0.00")


def test_boundary_remaining_limit_zero_gives_zero() -> None:
    """Task 6: Remaining limit 0 gives 0.00."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("5000.00"),
        remaining_limit=Decimal("0.00"),
        assessed_loss=Decimal("5000.00"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("0.00")


def test_boundary_copay_100_percent_gives_zero() -> None:
    """Task 6: Copay 100% gives 0.00."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("5000.00"),
        remaining_limit=Decimal("10000.00"),
        assessed_loss=Decimal("5000.00"),
        copay_percent=Decimal("100.00"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("0.00")


def test_boundary_depreciation_above_loss_gives_zero() -> None:
    """Task 6: Depreciation exceeding loss yields 0.00 cleanly without negative payable."""
    inp = AssessmentInput(
        product_type="VEHICLE",
        requested_amount=Decimal("5000.00"),
        remaining_limit=Decimal("10000.00"),
        assessed_loss=Decimal("5000.00"),
        depreciation=Decimal("8000.00"),
    )
    out = calculate_assessment_cap(inp)
    assert out.max_eligible_amount == Decimal("0.00")


def test_boundary_zero_or_tiny_value_at_risk() -> None:
    """Task 6: Zero or tiny value at risk must not crash with division by zero."""
    inp_zero = AssessmentInput(
        product_type="PROPERTY",
        requested_amount=Decimal("5000.00"),
        remaining_limit=Decimal("10000.00"),
        assessed_loss=Decimal("5000.00"),
        sum_insured=Decimal("10000.00"),
        value_at_risk=Decimal("0.00"),
    )
    out_zero = calculate_assessment_cap(inp_zero)
    assert out_zero.max_eligible_amount == Decimal("5000.00")

    inp_tiny = AssessmentInput(
        product_type="PROPERTY",
        requested_amount=Decimal("5000.00"),
        remaining_limit=Decimal("10000.00"),
        assessed_loss=Decimal("5000.00"),
        sum_insured=Decimal("10000.00"),
        value_at_risk=Decimal("0.01"),
    )
    out_tiny = calculate_assessment_cap(inp_tiny)
    assert out_tiny.max_eligible_amount == Decimal("5000.00")


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


def test_assessment_negative_input_validations() -> None:
    """Verify strictly that negative numbers in AssessmentInput raise ValueError."""
    valid_base = {
        "product_type": "HEALTH",
        "requested_amount": Decimal("1000.00"),
        "remaining_limit": Decimal("5000.00"),
        "assessed_loss": Decimal("1000.00"),
        "deductible": Decimal("100.00"),
        "copay_percent": Decimal("10.00"),
        "depreciation": Decimal("0.00"),
        "sum_insured": Decimal("5000.00"),
        "value_at_risk": Decimal("5000.00"),
    }

    # Negative requested amount
    with pytest.raises(ValueError, match="Requested amount cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**valid_base, "requested_amount": Decimal("-1.00")}))

    # Negative assessed loss
    with pytest.raises(ValueError, match="Assessed loss cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**valid_base, "assessed_loss": Decimal("-1.00")}))

    # Negative remaining limit
    with pytest.raises(ValueError, match="Remaining limit cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**valid_base, "remaining_limit": Decimal("-1.00")}))

    # Negative deductible
    with pytest.raises(ValueError, match="Deductible cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**valid_base, "deductible": Decimal("-1.00")}))

    # Negative copay
    with pytest.raises(ValueError, match="Copay percent must be between 0 and 100"):
        calculate_assessment_cap(AssessmentInput(**{**valid_base, "copay_percent": Decimal("-1.00")}))

    # Copay > 100
    with pytest.raises(ValueError, match="Copay percent must be between 0 and 100"):
        calculate_assessment_cap(AssessmentInput(**{**valid_base, "copay_percent": Decimal("101.00")}))

    # Negative depreciation
    with pytest.raises(ValueError, match="Depreciation cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**valid_base, "depreciation": Decimal("-1.00")}))

    # Negative sum insured
    with pytest.raises(ValueError, match="Sum insured cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**valid_base, "sum_insured": Decimal("-1.00")}))

    # Negative value at risk
    with pytest.raises(ValueError, match="Value at risk cannot be negative"):
        calculate_assessment_cap(AssessmentInput(**{**valid_base, "value_at_risk": Decimal("-1.00")}))


def test_assessment_component_validations() -> None:
    """Verify component amount validation rules."""
    # Negative component claimed or assessed amount
    with pytest.raises(ValueError, match="Component 'parts' amounts cannot be negative"):
        calculate_assessment_cap(
            AssessmentInput(
                product_type="VEHICLE",
                requested_amount=Decimal("1000.00"),
                remaining_limit=Decimal("5000.00"),
                assessed_loss=Decimal("1000.00"),
                components=(
                    AssessmentComponentInput(
                        component_type="parts",
                        claimed_amount=Decimal("-10.00"),
                        assessed_amount=Decimal("1000.00"),
                    ),
                ),
            )
        )

    # Component assessed amount > claimed amount
    with pytest.raises(ValueError, match="cannot exceed claimed amount"):
        calculate_assessment_cap(
            AssessmentInput(
                product_type="VEHICLE",
                requested_amount=Decimal("1000.00"),
                remaining_limit=Decimal("5000.00"),
                assessed_loss=Decimal("1000.00"),
                components=(
                    AssessmentComponentInput(
                        component_type="parts",
                        claimed_amount=Decimal("500.00"),
                        assessed_amount=Decimal("600.00"),
                    ),
                ),
            )
        )

    # Sum of component assessed amounts mismatch
    with pytest.raises(ValueError, match="does not match total assessed loss"):
        calculate_assessment_cap(
            AssessmentInput(
                product_type="VEHICLE",
                requested_amount=Decimal("1000.00"),
                remaining_limit=Decimal("5000.00"),
                assessed_loss=Decimal("1000.00"),
                components=(
                    AssessmentComponentInput(
                        component_type="parts",
                        claimed_amount=Decimal("500.00"),
                        assessed_amount=Decimal("500.00"),
                    ),
                ),
            )
        )

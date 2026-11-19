"""Pure deterministic assessment cap calculation engine.

Calculates maximum eligible claim amount across product profiles:
- HEALTH: net = max(0, loss - deductible); payable = min(rem_limit, net * (1 - copay/100))
- VEHICLE: after_depr = max(0, loss - depreciation); payable = min(rem_limit, max(0, after_depr - deductible))
- PROPERTY: adjusted = loss * min(1, sum_insured / value_at_risk); payable = min(rem_limit, max(0, adjusted - deductible))
"""

from decimal import ROUND_HALF_UP, Decimal

from app.engine.types import AssessmentInput, AssessmentOutput, round_money

ZERO = Decimal("0.00")
HUNDRED = Decimal("100.00")
FOUR_DECIMALS = Decimal("0.0001")


def calculate_assessment_cap(inp: AssessmentInput) -> AssessmentOutput:
    """Calculate the maximum eligible claim amount and calculation trace deterministically."""
    if inp.assessed_loss < ZERO:
        raise ValueError("Assessed loss cannot be negative")
    if inp.remaining_limit < ZERO:
        raise ValueError("Remaining limit cannot be negative")
    if inp.deductible < ZERO:
        raise ValueError("Deductible cannot be negative")
    if inp.copay_percent < ZERO or inp.copay_percent > HUNDRED:
        raise ValueError("Copay percent must be between 0 and 100")
    if inp.depreciation < ZERO:
        raise ValueError("Depreciation cannot be negative")
    if inp.sum_insured < ZERO:
        raise ValueError("Sum insured cannot be negative")
    if inp.value_at_risk < ZERO:
        raise ValueError("Value at risk cannot be negative")

    product_upper = inp.product_type.upper()
    trace: dict[str, str] = {
        "product_type": product_upper,
        "assessed_loss": str(round_money(inp.assessed_loss)),
        "remaining_limit": str(round_money(inp.remaining_limit)),
        "deductible": str(round_money(inp.deductible)),
    }

    if product_upper == "HEALTH":
        net_after_deductible = max(ZERO, inp.assessed_loss - inp.deductible)
        copay_multiplier = Decimal(1) - (inp.copay_percent / HUNDRED)
        after_copay = net_after_deductible * copay_multiplier
        payable = min(inp.remaining_limit, after_copay)
        max_eligible = max(ZERO, round_money(payable))

        trace.update(
            {
                "copay_percent": str(inp.copay_percent),
                "net_after_deductible": str(round_money(net_after_deductible)),
                "after_copay": str(round_money(after_copay)),
                "payable": str(round_money(payable)),
                "max_eligible_amount": str(max_eligible),
            }
        )
    elif product_upper == "VEHICLE":
        after_depr = max(ZERO, inp.assessed_loss - inp.depreciation)
        after_deductible = max(ZERO, after_depr - inp.deductible)
        payable = min(inp.remaining_limit, after_deductible)
        max_eligible = max(ZERO, round_money(payable))

        trace.update(
            {
                "depreciation": str(round_money(inp.depreciation)),
                "after_depreciation": str(round_money(after_depr)),
                "after_deductible": str(round_money(after_deductible)),
                "payable": str(round_money(payable)),
                "max_eligible_amount": str(max_eligible),
            }
        )
    elif product_upper == "PROPERTY":
        if inp.value_at_risk > ZERO:
            coinsurance_factor = min(Decimal(1), inp.sum_insured / inp.value_at_risk)
        else:
            coinsurance_factor = Decimal(1)

        adjusted_loss = inp.assessed_loss * coinsurance_factor
        after_deductible = max(ZERO, adjusted_loss - inp.deductible)
        payable = min(inp.remaining_limit, after_deductible)
        max_eligible = max(ZERO, round_money(payable))

        trace.update(
            {
                "sum_insured": str(round_money(inp.sum_insured)),
                "value_at_risk": str(round_money(inp.value_at_risk)),
                "coinsurance_factor": str(
                    coinsurance_factor.quantize(FOUR_DECIMALS, rounding=ROUND_HALF_UP)
                ),
                "adjusted_loss": str(round_money(adjusted_loss)),
                "after_deductible": str(round_money(after_deductible)),
                "payable": str(round_money(payable)),
                "max_eligible_amount": str(max_eligible),
            }
        )
    else:
        # Default fallback
        net = max(ZERO, inp.assessed_loss - inp.deductible)
        payable = min(inp.remaining_limit, net)
        max_eligible = max(ZERO, round_money(payable))
        trace.update(
            {
                "net_after_deductible": str(round_money(net)),
                "payable": str(round_money(payable)),
                "max_eligible_amount": str(max_eligible),
            }
        )

    return AssessmentOutput(
        product_type=product_upper,
        assessed_loss=round_money(inp.assessed_loss),
        max_eligible_amount=max_eligible,
        calculation_trace=trace,
    )

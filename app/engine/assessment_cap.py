"""Pure deterministic assessment cap calculation engine (P5 Tasks 1, 2, 3, 4, 6).

Calculates maximum eligible claim amount across product profiles:
- HEALTH: net = max(0, loss - deductible); after_copay = net * (1 - copay/100); payable = min(rem_limit, after_copay)
- VEHICLE: after_depr = max(0, loss - depreciation); payable = min(rem_limit, max(0, after_depr - deductible))
- PROPERTY: adjusted = loss * min(1, sum_insured / value_at_risk); payable = min(rem_limit, max(0, adjusted - deductible))

All formulas are labeled FORMULA_SOURCE = "ILLUSTRATIVE_DEFAULTS" pending insurer sign-off (D11).
Float inputs are strictly rejected. Intermediate calculations maintain full Decimal precision.
Rounding occurs exactly once at the final step using ROUND_HALF_UP to 2 decimal places.
"""

from decimal import ROUND_HALF_UP, Decimal

from app.engine.product_profiles import (
    CALCULATION_SPEC_STATUS,
    FORMULA_SOURCE,
    ProductTypeEnum,
    get_product_profile,
)
from app.engine.types import AssessmentInput, AssessmentOutput, round_money

ZERO = Decimal("0.00")
HUNDRED = Decimal("100.00")
FOUR_DECIMALS = Decimal("0.0001")


def _reject_floats(*values: object) -> None:
    """Strictly reject float types to avoid precision loss."""
    for val in values:
        if isinstance(val, float):
            raise TypeError(
                f"Float value '{val}' rejected. All monetary and rate values must be Decimal or string."
            )


def calculate_assessment_cap(inp: AssessmentInput) -> AssessmentOutput:
    """Calculate the maximum eligible claim amount and calculation trace deterministically."""
    # 1. Type and value validations
    _reject_floats(
        inp.requested_amount,
        inp.remaining_limit,
        inp.assessed_loss,
        inp.deductible,
        inp.copay_percent,
        inp.depreciation,
        inp.sum_insured,
        inp.value_at_risk,
    )

    if inp.requested_amount < ZERO:
        raise ValueError("Requested amount cannot be negative")
    if inp.assessed_loss < ZERO:
        raise ValueError("Assessed loss cannot be negative")
    if inp.assessed_loss > inp.requested_amount:
        raise ValueError(
            f"Assessed loss ({inp.assessed_loss}) cannot exceed requested amount ({inp.requested_amount})"
        )
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

    # 2. Validate components if provided
    if inp.components:
        comp_assessed_sum = ZERO
        for comp in inp.components:
            _reject_floats(comp.claimed_amount, comp.assessed_amount)
            if comp.claimed_amount < ZERO or comp.assessed_amount < ZERO:
                raise ValueError(f"Component '{comp.component_type}' amounts cannot be negative")
            if comp.assessed_amount > comp.claimed_amount:
                raise ValueError(
                    f"Component '{comp.component_type}' assessed amount ({comp.assessed_amount}) "
                    f"cannot exceed claimed amount ({comp.claimed_amount})"
                )
            if comp.claimed_amount > ZERO and comp.assessed_amount == ZERO:
                if not comp.zeroed_reason or not comp.zeroed_reason.strip():
                    raise ValueError(
                        f"Component '{comp.component_type}' was zeroed out but no zeroed_reason was provided"
                    )
            comp_assessed_sum += comp.assessed_amount

        if comp_assessed_sum != inp.assessed_loss:
            raise ValueError(
                f"Sum of component assessed amounts ({comp_assessed_sum}) "
                f"does not match total assessed loss ({inp.assessed_loss})"
            )

    # 3. Resolve product profile (rejects unsupported products; no generic fallback)
    profile = get_product_profile(inp.product_type)
    product_key = profile.product_type.value

    trace: dict[str, object] = {
        "product_type": product_key,
        "profile_name": profile.name,
        "formula_source": FORMULA_SOURCE,
        "calculation_spec_status": CALCULATION_SPEC_STATUS,
        "ordered_steps": list(profile.ordered_steps),
        "requested_amount": str(round_money(inp.requested_amount)),
        "assessed_loss": str(round_money(inp.assessed_loss)),
        "remaining_limit": str(round_money(inp.remaining_limit)),
        "deductible": str(round_money(inp.deductible)),
    }

    if inp.components:
        trace["components"] = [
            {
                "type": c.component_type,
                "claimed": str(round_money(c.claimed_amount)),
                "assessed": str(round_money(c.assessed_amount)),
                "zeroed_reason": c.zeroed_reason,
            }
            for c in inp.components
        ]

    # 4. Execute product-specific ordered calculation steps
    if profile.product_type == ProductTypeEnum.HEALTH:
        # Step 1: Deductible
        net_after_deductible = max(ZERO, inp.assessed_loss - inp.deductible)
        # Step 2: Copay
        copay_multiplier = Decimal(1) - (inp.copay_percent / HUNDRED)
        after_copay = net_after_deductible * copay_multiplier
        # Step 3: Limit Cap
        limit_capped = after_copay > inp.remaining_limit
        payable = min(inp.remaining_limit, after_copay)
        shortfall = max(ZERO, after_copay - inp.remaining_limit) if limit_capped else ZERO
        max_eligible = max(ZERO, round_money(payable))

        trace.update(
            {
                "net_after_deductible": str(round_money(net_after_deductible)),
                "after_copay": str(round_money(after_copay)),
                "step_1_deductible": {
                    "deductible": str(round_money(inp.deductible)),
                    "net_after_deductible": str(round_money(net_after_deductible)),
                },
                "step_2_copay": {
                    "copay_percent": str(inp.copay_percent),
                    "after_copay": str(round_money(after_copay)),
                },
                "step_3_limit_cap": {
                    "remaining_limit": str(round_money(inp.remaining_limit)),
                    "limit_capped": limit_capped,
                    "shortfall": str(round_money(shortfall)),
                    "cap_reason": "LIMIT_APPLIED" if limit_capped else None,
                },
                "payable": str(round_money(payable)),
                "max_eligible_amount": str(max_eligible),
            }
        )

    elif profile.product_type == ProductTypeEnum.VEHICLE:
        # Step 1: Depreciation (applied to parts if components supplied)
        if inp.components:
            parts_assessed = sum(
                (
                    c.assessed_amount
                    for c in inp.components
                    if c.component_type.upper() in ("PARTS", "SPARE_PARTS")
                ),
                ZERO,
            )
            labour_assessed = sum(
                (
                    c.assessed_amount
                    for c in inp.components
                    if c.component_type.upper() not in ("PARTS", "SPARE_PARTS")
                ),
                ZERO,
            )
            parts_depr = min(parts_assessed, inp.depreciation)
            after_depr = (parts_assessed - parts_depr) + labour_assessed
        else:
            depr_applied = min(inp.assessed_loss, inp.depreciation)
            after_depr = max(ZERO, inp.assessed_loss - depr_applied)
            parts_depr = inp.depreciation

        # Step 2: Deductible
        after_deductible = max(ZERO, after_depr - inp.deductible)

        # Step 3: Limit Cap
        limit_capped = after_deductible > inp.remaining_limit
        payable = min(inp.remaining_limit, after_deductible)
        shortfall = max(ZERO, after_deductible - inp.remaining_limit) if limit_capped else ZERO
        max_eligible = max(ZERO, round_money(payable))

        trace.update(
            {
                "after_depreciation": str(round_money(after_depr)),
                "after_deductible": str(round_money(after_deductible)),
                "step_1_depreciation": {
                    "depreciation_input": str(round_money(inp.depreciation)),
                    "depreciation_applied": str(round_money(parts_depr)),
                    "after_depreciation": str(round_money(after_depr)),
                },
                "step_2_deductible": {
                    "deductible": str(round_money(inp.deductible)),
                    "after_deductible": str(round_money(after_deductible)),
                },
                "step_3_limit_cap": {
                    "remaining_limit": str(round_money(inp.remaining_limit)),
                    "limit_capped": limit_capped,
                    "shortfall": str(round_money(shortfall)),
                    "cap_reason": "LIMIT_APPLIED" if limit_capped else None,
                },
                "payable": str(round_money(payable)),
                "max_eligible_amount": str(max_eligible),
            }
        )

    elif profile.product_type == ProductTypeEnum.PROPERTY:
        # Step 1: Coinsurance Factor min(1, sum_insured / value_at_risk)
        if inp.value_at_risk > ZERO:
            coinsurance_factor = min(Decimal(1), inp.sum_insured / inp.value_at_risk)
        else:
            coinsurance_factor = Decimal(1)

        adjusted_loss = inp.assessed_loss * coinsurance_factor

        # Step 2: Deductible
        after_deductible = max(ZERO, adjusted_loss - inp.deductible)

        # Step 3: Limit Cap
        limit_capped = after_deductible > inp.remaining_limit
        payable = min(inp.remaining_limit, after_deductible)
        shortfall = max(ZERO, after_deductible - inp.remaining_limit) if limit_capped else ZERO
        max_eligible = max(ZERO, round_money(payable))

        trace.update(
            {
                "coinsurance_factor": str(
                    coinsurance_factor.quantize(FOUR_DECIMALS, rounding=ROUND_HALF_UP)
                ),
                "adjusted_loss": str(round_money(adjusted_loss)),
                "after_deductible": str(round_money(after_deductible)),
                "step_1_coinsurance": {
                    "sum_insured": str(round_money(inp.sum_insured)),
                    "value_at_risk": str(round_money(inp.value_at_risk)),
                    "coinsurance_factor": str(
                        coinsurance_factor.quantize(FOUR_DECIMALS, rounding=ROUND_HALF_UP)
                    ),
                    "adjusted_loss": str(round_money(adjusted_loss)),
                },
                "step_2_deductible": {
                    "deductible": str(round_money(inp.deductible)),
                    "after_deductible": str(round_money(after_deductible)),
                },
                "step_3_limit_cap": {
                    "remaining_limit": str(round_money(inp.remaining_limit)),
                    "limit_capped": limit_capped,
                    "shortfall": str(round_money(shortfall)),
                    "cap_reason": "LIMIT_APPLIED" if limit_capped else None,
                },
                "payable": str(round_money(payable)),
                "max_eligible_amount": str(max_eligible),
            }
        )

    return AssessmentOutput(
        product_type=product_key,
        assessed_loss=round_money(inp.assessed_loss),
        max_eligible_amount=max_eligible,
        calculation_trace=trace,
    )

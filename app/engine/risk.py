"""Pure deterministic risk indicator evaluation engine.

Implements exact order of operations:
1. Load ruleset and engine version.
2. Build typed RiskInput (zero protected attributes).
3. Evaluate indicators in fixed ID order R01-R10.
4. Apply group caps.
5. Aggregate score (min 100).
6. Calculate completeness.
7. Assign category (UNASSESSED with NULL score if INSUFFICIENT_DATA, never LOW).
8. Compute routing (score >= threshold or sufficient_alone).
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

from app.engine.types import (
    DocumentInput,
    HistoricalClaimInput,
    IndicatorEvaluationStatusEnum,
    RiskCategoryEnum,
    RiskCompletenessEnum,
    RiskIndicatorResult,
    RiskInput,
    RiskOutput,
    round_money,
)


def evaluate_risk(risk_input: RiskInput) -> RiskOutput:
    """Evaluate pure deterministic risk indicators R01-R10 and compute aggregated score."""
    # 1. Load ruleset and engine version
    ruleset: dict[str, Any] = risk_input.ruleset
    ruleset_ver: str = ruleset.get("ruleset_version", "v1")
    engine_ver: str = ruleset.get("engine_version", "1.0.0")

    groups_cfg: dict[str, Any] = ruleset.get("risk_groups", {})
    indicators_cfg: dict[str, Any] = ruleset.get("risk_indicators", {})
    thresholds_cfg: dict[str, Any] = ruleset.get("risk_thresholds", {})
    pilot_banner: str = thresholds_cfg.get(
        "pilot_banner",
        "PILOT: Risk indicators are experimental and unvalidated. Manual review required.",
    )

    # 2. Extract inputs
    claim = risk_input.claim
    policy = risk_input.policy
    coverage = risk_input.coverage
    eval_date = risk_input.evaluation_date
    history_cov = risk_input.history_coverage
    product_type = policy.product_type if policy else "HEALTH"

    # Ensure deterministic iteration order across input collections
    sorted_history: list[HistoricalClaimInput] = sorted(
        risk_input.historical_claims, key=lambda c: (c.incident_date, c.claim_id)
    )
    sorted_docs: list[DocumentInput] = sorted(risk_input.documents, key=lambda d: d.document_id)

    results: list[RiskIndicatorResult] = []

    # Helper to check common indicator preconditions (enabled, applicable_products)
    def _get_common_cfg(ind_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], indicators_cfg.get(ind_id, {}))

    # ==========================================
    # 3. Evaluate indicators in fixed ID order R01 to R10
    # ==========================================

    # --- R01: Repeated incidents by customer ---
    cfg_r01 = _get_common_cfg("R01")
    r01_ver = str(cfg_r01.get("version", "1.0"))
    r01_group = str(cfg_r01.get("group", "FREQUENCY"))
    r01_weight = int(cfg_r01.get("weight", 20))
    r01_enabled = bool(cfg_r01.get("enabled", True))
    r01_suff = bool(cfg_r01.get("sufficient_alone", False))
    r01_ev_level = str(cfg_r01.get("evidence_level", "NONE"))
    r01_products = cfg_r01.get("applicable_products", ["HEALTH", "VEHICLE", "PROPERTY"])
    threshold_inc_r01 = int(cfg_r01.get("threshold_incidents", cfg_r01.get("threshold_claims", 3)))
    window_days_r01 = int(cfg_r01.get("window_days", 365))

    if not r01_enabled:
        results.append(
            RiskIndicatorResult(
                indicator_id="R01",
                indicator_version=r01_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.DISABLED,
                group=r01_group,
                weight=r01_weight,
                sufficient_alone=r01_suff,
                evidence_level=r01_ev_level,
                evidence={},
                message="Indicator R01 disabled in active ruleset",
            )
        )
    elif product_type not in r01_products:
        results.append(
            RiskIndicatorResult(
                indicator_id="R01",
                indicator_version=r01_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
                group=r01_group,
                weight=r01_weight,
                sufficient_alone=r01_suff,
                evidence_level=r01_ev_level,
                evidence={"product_type": product_type, "applicable_products": r01_products},
                message=f"Indicator R01 not applicable to product {product_type}",
            )
        )
    else:
        window_start_r01 = eval_date - timedelta(days=window_days_r01)
        loaded_from_r01 = history_cov.get(product_type)
        if loaded_from_r01 and window_start_r01 < loaded_from_r01:
            results.append(
                RiskIndicatorResult(
                    indicator_id="R01",
                    indicator_version=r01_ver,
                    triggered=False,
                    weight_applied=0,
                    status=IndicatorEvaluationStatusEnum.SKIPPED,
                    group=r01_group,
                    weight=r01_weight,
                    sufficient_alone=r01_suff,
                    evidence_level=r01_ev_level,
                    skipped=True,
                    skip_reason="HISTORY_WINDOW_NOT_COVERED",
                    evidence={
                        "window_start": window_start_r01.isoformat(),
                        "loaded_from": loaded_from_r01.isoformat(),
                        "product_type": product_type,
                    },
                    message="Skipped: history coverage does not cover the required lookback window",
                )
            )
        else:
            qualifying_cust_claims = [
                c
                for c in sorted_history
                if c.customer_id == claim.customer_id
                and c.claim_id != claim.claim_id
                and c.status != "WITHDRAWN"
                and 0 <= (eval_date - c.registered_date).days <= window_days_r01
            ]
            distinct_incidents_r01 = {
                c.incident_group_id if c.incident_group_id else c.claim_id
                for c in qualifying_cust_claims
            }
            incidents_count_r01 = len(distinct_incidents_r01)
            claims_count_r01 = len(qualifying_cust_claims)
            r01_triggered = incidents_count_r01 >= threshold_inc_r01

            results.append(
                RiskIndicatorResult(
                    indicator_id="R01",
                    indicator_version=r01_ver,
                    triggered=r01_triggered,
                    weight_applied=r01_weight if r01_triggered else 0,
                    status=(
                        IndicatorEvaluationStatusEnum.TRIGGERED
                        if r01_triggered
                        else IndicatorEvaluationStatusEnum.NOT_TRIGGERED
                    ),
                    group=r01_group,
                    weight=r01_weight,
                    sufficient_alone=r01_suff,
                    evidence_level=r01_ev_level,
                    evidence={
                        "prior_qualifying_claims_count": claims_count_r01,
                        "prior_qualifying_incidents_count": incidents_count_r01,
                        "prior_claim_refs": sorted([c.claim_ref for c in qualifying_cust_claims]),
                        "threshold": threshold_inc_r01,
                        "window_days": window_days_r01,
                    },
                    message=(
                        f"Customer filed claims across {incidents_count_r01} distinct incident(s) in past {window_days_r01} days"
                        if r01_triggered
                        else "Customer incident frequency within expected parameters"
                    ),
                )
            )

    # --- R02: Repeated incidents on policy ---
    cfg_r02 = _get_common_cfg("R02")
    r02_ver = str(cfg_r02.get("version", "1.0"))
    r02_group = str(cfg_r02.get("group", "FREQUENCY"))
    r02_weight = int(cfg_r02.get("weight", 15))
    r02_enabled = bool(cfg_r02.get("enabled", True))
    r02_suff = bool(cfg_r02.get("sufficient_alone", False))
    r02_ev_level = str(cfg_r02.get("evidence_level", "NONE"))
    r02_products = cfg_r02.get("applicable_products", ["HEALTH", "VEHICLE", "PROPERTY"])
    threshold_inc_r02 = int(cfg_r02.get("threshold_incidents", cfg_r02.get("threshold_claims", 2)))
    window_days_r02 = int(cfg_r02.get("window_days", 180))

    if not r02_enabled:
        results.append(
            RiskIndicatorResult(
                indicator_id="R02",
                indicator_version=r02_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.DISABLED,
                group=r02_group,
                weight=r02_weight,
                sufficient_alone=r02_suff,
                evidence_level=r02_ev_level,
                evidence={},
                message="Indicator R02 disabled in active ruleset",
            )
        )
    elif product_type not in r02_products:
        results.append(
            RiskIndicatorResult(
                indicator_id="R02",
                indicator_version=r02_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
                group=r02_group,
                weight=r02_weight,
                sufficient_alone=r02_suff,
                evidence_level=r02_ev_level,
                evidence={"product_type": product_type, "applicable_products": r02_products},
                message=f"Indicator R02 not applicable to product {product_type}",
            )
        )
    else:
        window_start_r02 = eval_date - timedelta(days=window_days_r02)
        loaded_from_r02 = history_cov.get(product_type)
        if loaded_from_r02 and window_start_r02 < loaded_from_r02:
            results.append(
                RiskIndicatorResult(
                    indicator_id="R02",
                    indicator_version=r02_ver,
                    triggered=False,
                    weight_applied=0,
                    status=IndicatorEvaluationStatusEnum.SKIPPED,
                    group=r02_group,
                    weight=r02_weight,
                    sufficient_alone=r02_suff,
                    evidence_level=r02_ev_level,
                    skipped=True,
                    skip_reason="HISTORY_WINDOW_NOT_COVERED",
                    evidence={
                        "window_start": window_start_r02.isoformat(),
                        "loaded_from": loaded_from_r02.isoformat(),
                        "product_type": product_type,
                    },
                    message="Skipped: history coverage does not cover the required lookback window",
                )
            )
        else:
            qualifying_pol_claims = [
                c
                for c in sorted_history
                if c.policy_id == claim.policy_id
                and c.claim_id != claim.claim_id
                and c.status != "WITHDRAWN"
                and 0 <= (eval_date - c.registered_date).days <= window_days_r02
            ]
            distinct_incidents_r02 = {
                c.incident_group_id if c.incident_group_id else c.claim_id
                for c in qualifying_pol_claims
            }
            incidents_count_r02 = len(distinct_incidents_r02)
            claims_count_r02 = len(qualifying_pol_claims)
            r02_triggered = incidents_count_r02 >= threshold_inc_r02

            results.append(
                RiskIndicatorResult(
                    indicator_id="R02",
                    indicator_version=r02_ver,
                    triggered=r02_triggered,
                    weight_applied=r02_weight if r02_triggered else 0,
                    status=(
                        IndicatorEvaluationStatusEnum.TRIGGERED
                        if r02_triggered
                        else IndicatorEvaluationStatusEnum.NOT_TRIGGERED
                    ),
                    group=r02_group,
                    weight=r02_weight,
                    sufficient_alone=r02_suff,
                    evidence_level=r02_ev_level,
                    evidence={
                        "prior_qualifying_claims_count": claims_count_r02,
                        "prior_qualifying_incidents_count": incidents_count_r02,
                        "prior_claim_refs": sorted([c.claim_ref for c in qualifying_pol_claims]),
                        "threshold": threshold_inc_r02,
                        "window_days": window_days_r02,
                    },
                    message=(
                        f"Policy has claims across {incidents_count_r02} distinct incident(s) in past {window_days_r02} days"
                        if r02_triggered
                        else "Policy incident frequency within expected parameters"
                    ),
                )
            )

    # --- R03: High amount vs limit ---
    cfg_r03 = _get_common_cfg("R03")
    r03_ver = str(cfg_r03.get("version", "1.0"))
    r03_group = str(cfg_r03.get("group", "AMOUNT"))
    r03_weight = int(cfg_r03.get("weight", 20))
    r03_enabled = bool(cfg_r03.get("enabled", True))
    r03_suff = bool(cfg_r03.get("sufficient_alone", False))
    r03_ev_level = str(cfg_r03.get("evidence_level", "NONE"))
    r03_products = cfg_r03.get("applicable_products", ["HEALTH", "VEHICLE", "PROPERTY"])
    ratio_threshold_pct = Decimal(str(cfg_r03.get("limit_ratio_threshold_percent", 80)))

    if not r03_enabled:
        results.append(
            RiskIndicatorResult(
                indicator_id="R03",
                indicator_version=r03_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.DISABLED,
                group=r03_group,
                weight=r03_weight,
                sufficient_alone=r03_suff,
                evidence_level=r03_ev_level,
                evidence={},
                message="Indicator R03 disabled in active ruleset",
            )
        )
    elif product_type not in r03_products:
        results.append(
            RiskIndicatorResult(
                indicator_id="R03",
                indicator_version=r03_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
                group=r03_group,
                weight=r03_weight,
                sufficient_alone=r03_suff,
                evidence_level=r03_ev_level,
                evidence={"product_type": product_type, "applicable_products": r03_products},
                message=f"Indicator R03 not applicable to product {product_type}",
            )
        )
    elif coverage is None:
        results.append(
            RiskIndicatorResult(
                indicator_id="R03",
                indicator_version=r03_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.SKIPPED,
                group=r03_group,
                weight=r03_weight,
                sufficient_alone=r03_suff,
                evidence_level=r03_ev_level,
                skipped=True,
                skip_reason="Coverage details missing",
                evidence={},
                message="Skipped because matching coverage terms were not found",
            )
        )
    else:
        remaining_limit = max(Decimal("0.00"), coverage.limit_amount - coverage.consumed_amount)
        threshold_amount = round_money(remaining_limit * (ratio_threshold_pct / Decimal("100.00")))
        r03_triggered = claim.requested_amount > threshold_amount

        results.append(
            RiskIndicatorResult(
                indicator_id="R03",
                indicator_version=r03_ver,
                triggered=r03_triggered,
                weight_applied=r03_weight if r03_triggered else 0,
                status=(
                    IndicatorEvaluationStatusEnum.TRIGGERED
                    if r03_triggered
                    else IndicatorEvaluationStatusEnum.NOT_TRIGGERED
                ),
                group=r03_group,
                weight=r03_weight,
                sufficient_alone=r03_suff,
                evidence_level=r03_ev_level,
                evidence={
                    "remaining_limit": str(remaining_limit),
                    "requested_amount": str(claim.requested_amount),
                    "ratio_threshold_percent": str(ratio_threshold_pct),
                    "threshold_amount": str(threshold_amount),
                },
                message=(
                    f"Requested amount exceeds {ratio_threshold_pct}% of remaining limit"
                    if r03_triggered
                    else "Requested amount within standard limit proportion"
                ),
            )
        )

    # --- R04: Unusual amount vs peers ---
    cfg_r04 = _get_common_cfg("R04")
    r04_ver = str(cfg_r04.get("version", "1.0"))
    r04_group = str(cfg_r04.get("group", "AMOUNT"))
    r04_weight = int(cfg_r04.get("weight", 15))
    r04_enabled = bool(cfg_r04.get("enabled", True))
    r04_suff = bool(cfg_r04.get("sufficient_alone", False))
    r04_ev_level = str(cfg_r04.get("evidence_level", "NONE"))
    r04_products = cfg_r04.get("applicable_products", ["HEALTH", "VEHICLE", "PROPERTY"])
    peer_mult = Decimal(str(cfg_r04.get("peer_multiple_threshold", 3)))
    min_peer_claims = int(cfg_r04.get("min_historical_claims", 10))
    peer_amounts = sorted(risk_input.peer_approved_amounts)

    if not r04_enabled:
        results.append(
            RiskIndicatorResult(
                indicator_id="R04",
                indicator_version=r04_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.DISABLED,
                group=r04_group,
                weight=r04_weight,
                sufficient_alone=r04_suff,
                evidence_level=r04_ev_level,
                evidence={},
                message="Indicator R04 disabled in active ruleset",
            )
        )
    elif product_type not in r04_products:
        results.append(
            RiskIndicatorResult(
                indicator_id="R04",
                indicator_version=r04_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
                group=r04_group,
                weight=r04_weight,
                sufficient_alone=r04_suff,
                evidence_level=r04_ev_level,
                evidence={"product_type": product_type, "applicable_products": r04_products},
                message=f"Indicator R04 not applicable to product {product_type}",
            )
        )
    elif len(peer_amounts) < min_peer_claims:
        results.append(
            RiskIndicatorResult(
                indicator_id="R04",
                indicator_version=r04_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.SKIPPED,
                group=r04_group,
                weight=r04_weight,
                sufficient_alone=r04_suff,
                evidence_level=r04_ev_level,
                skipped=True,
                skip_reason="INSUFFICIENT_PEER_HISTORY",
                evidence={"available_peer_claims": len(peer_amounts), "required": min_peer_claims},
                message=f"Skipped due to insufficient peer baseline data ({len(peer_amounts)} historical claims)",
            )
        )
    else:
        n = len(peer_amounts)
        if n % 2 == 1:
            median_amt = peer_amounts[n // 2]
        else:
            median_amt = round_money(
                (peer_amounts[n // 2 - 1] + peer_amounts[n // 2]) / Decimal("2")
            )
        threshold_peer_amt = round_money(median_amt * peer_mult)
        r04_triggered = claim.requested_amount > threshold_peer_amt

        results.append(
            RiskIndicatorResult(
                indicator_id="R04",
                indicator_version=r04_ver,
                triggered=r04_triggered,
                weight_applied=r04_weight if r04_triggered else 0,
                status=(
                    IndicatorEvaluationStatusEnum.TRIGGERED
                    if r04_triggered
                    else IndicatorEvaluationStatusEnum.NOT_TRIGGERED
                ),
                group=r04_group,
                weight=r04_weight,
                sufficient_alone=r04_suff,
                evidence_level=r04_ev_level,
                evidence={
                    "peer_claim_count": n,
                    "peer_median_amount": str(median_amt),
                    "peer_multiple_threshold": str(peer_mult),
                    "threshold_amount": str(threshold_peer_amt),
                    "requested_amount": str(claim.requested_amount),
                },
                message=(
                    f"Requested amount exceeds {peer_mult}x peer median of {median_amt}"
                    if r04_triggered
                    else "Requested amount consistent with category peer median"
                ),
            )
        )

    # --- R05: Early claim ---
    cfg_r05 = _get_common_cfg("R05")
    r05_ver = str(cfg_r05.get("version", "1.0"))
    r05_group = str(cfg_r05.get("group", "TIMING"))
    r05_weight = int(cfg_r05.get("weight", 15))
    r05_enabled = bool(cfg_r05.get("enabled", True))
    r05_suff = bool(cfg_r05.get("sufficient_alone", False))
    r05_ev_level = str(cfg_r05.get("evidence_level", "NONE"))
    r05_products = cfg_r05.get("applicable_products", ["HEALTH", "VEHICLE", "PROPERTY"])
    early_window = int(cfg_r05.get("early_claim_window_days", 30))

    if not r05_enabled:
        results.append(
            RiskIndicatorResult(
                indicator_id="R05",
                indicator_version=r05_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.DISABLED,
                group=r05_group,
                weight=r05_weight,
                sufficient_alone=r05_suff,
                evidence_level=r05_ev_level,
                evidence={},
                message="Indicator R05 disabled in active ruleset",
            )
        )
    elif product_type not in r05_products:
        results.append(
            RiskIndicatorResult(
                indicator_id="R05",
                indicator_version=r05_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
                group=r05_group,
                weight=r05_weight,
                sufficient_alone=r05_suff,
                evidence_level=r05_ev_level,
                evidence={"product_type": product_type, "applicable_products": r05_products},
                message=f"Indicator R05 not applicable to product {product_type}",
            )
        )
    elif policy is None:
        results.append(
            RiskIndicatorResult(
                indicator_id="R05",
                indicator_version=r05_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.SKIPPED,
                group=r05_group,
                weight=r05_weight,
                sufficient_alone=r05_suff,
                evidence_level=r05_ev_level,
                skipped=True,
                skip_reason="Policy details missing",
                evidence={},
                message="Skipped because policy details were not found",
            )
        )
    else:
        days_since_start = (claim.incident_date - policy.start_date).days
        r05_triggered = 0 <= days_since_start <= early_window

        results.append(
            RiskIndicatorResult(
                indicator_id="R05",
                indicator_version=r05_ver,
                triggered=r05_triggered,
                weight_applied=r05_weight if r05_triggered else 0,
                status=(
                    IndicatorEvaluationStatusEnum.TRIGGERED
                    if r05_triggered
                    else IndicatorEvaluationStatusEnum.NOT_TRIGGERED
                ),
                group=r05_group,
                weight=r05_weight,
                sufficient_alone=r05_suff,
                evidence_level=r05_ev_level,
                evidence={
                    "incident_date": claim.incident_date.isoformat(),
                    "policy_start_date": policy.start_date.isoformat(),
                    "days_since_policy_start": days_since_start,
                    "window_days": early_window,
                },
                message=(
                    f"Incident occurred within {days_since_start} days of policy start (window: {early_window} days)"
                    if r05_triggered
                    else "Incident timing conforms to standard policy tenure"
                ),
            )
        )

    # --- R06: Late-in-policy claim ---
    cfg_r06 = _get_common_cfg("R06")
    r06_ver = str(cfg_r06.get("version", "1.0"))
    r06_group = str(cfg_r06.get("group", "TIMING"))
    r06_weight = int(cfg_r06.get("weight", 10))
    r06_enabled = bool(cfg_r06.get("enabled", True))
    r06_suff = bool(cfg_r06.get("sufficient_alone", False))
    r06_ev_level = str(cfg_r06.get("evidence_level", "NONE"))
    r06_products = cfg_r06.get("applicable_products", ["HEALTH", "VEHICLE", "PROPERTY"])
    late_window = int(cfg_r06.get("late_policy_window_days", 7))

    if not r06_enabled:
        results.append(
            RiskIndicatorResult(
                indicator_id="R06",
                indicator_version=r06_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.DISABLED,
                group=r06_group,
                weight=r06_weight,
                sufficient_alone=r06_suff,
                evidence_level=r06_ev_level,
                evidence={},
                message="Indicator R06 disabled in active ruleset",
            )
        )
    elif product_type not in r06_products:
        results.append(
            RiskIndicatorResult(
                indicator_id="R06",
                indicator_version=r06_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
                group=r06_group,
                weight=r06_weight,
                sufficient_alone=r06_suff,
                evidence_level=r06_ev_level,
                evidence={"product_type": product_type, "applicable_products": r06_products},
                message=f"Indicator R06 not applicable to product {product_type}",
            )
        )
    elif policy is None:
        results.append(
            RiskIndicatorResult(
                indicator_id="R06",
                indicator_version=r06_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.SKIPPED,
                group=r06_group,
                weight=r06_weight,
                sufficient_alone=r06_suff,
                evidence_level=r06_ev_level,
                skipped=True,
                skip_reason="Policy details missing",
                evidence={},
                message="Skipped because policy details were not found",
            )
        )
    else:
        days_before_end = (policy.end_date - claim.incident_date).days
        r06_triggered = 0 <= days_before_end <= late_window

        results.append(
            RiskIndicatorResult(
                indicator_id="R06",
                indicator_version=r06_ver,
                triggered=r06_triggered,
                weight_applied=r06_weight if r06_triggered else 0,
                status=(
                    IndicatorEvaluationStatusEnum.TRIGGERED
                    if r06_triggered
                    else IndicatorEvaluationStatusEnum.NOT_TRIGGERED
                ),
                group=r06_group,
                weight=r06_weight,
                sufficient_alone=r06_suff,
                evidence_level=r06_ev_level,
                evidence={
                    "incident_date": claim.incident_date.isoformat(),
                    "policy_end_date": policy.end_date.isoformat(),
                    "days_before_policy_end": days_before_end,
                    "window_days": late_window,
                },
                message=(
                    f"Incident occurred {days_before_end} days before policy expiration (window: {late_window} days)"
                    if r06_triggered
                    else "Incident occurred well before policy end date"
                ),
            )
        )

    # --- R07: Duplicate suspicion ---
    cfg_r07 = _get_common_cfg("R07")
    r07_ver = str(cfg_r07.get("version", "1.0"))
    r07_group = str(cfg_r07.get("group", "DUPLICATE"))
    r07_weight = int(cfg_r07.get("weight", 10))
    r07_enabled = bool(cfg_r07.get("enabled", True))
    r07_suff = bool(cfg_r07.get("sufficient_alone", True))
    r07_ev_level = str(cfg_r07.get("evidence_level", "NONE"))
    r07_products = cfg_r07.get("applicable_products", ["HEALTH", "VEHICLE", "PROPERTY"])
    variance_pct_threshold = Decimal(str(cfg_r07.get("amount_variance_percent", 5)))

    if not r07_enabled:
        results.append(
            RiskIndicatorResult(
                indicator_id="R07",
                indicator_version=r07_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.DISABLED,
                group=r07_group,
                weight=r07_weight,
                sufficient_alone=r07_suff,
                evidence_level=r07_ev_level,
                evidence={},
                message="Indicator R07 disabled in active ruleset",
            )
        )
    elif product_type not in r07_products:
        results.append(
            RiskIndicatorResult(
                indicator_id="R07",
                indicator_version=r07_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
                group=r07_group,
                weight=r07_weight,
                sufficient_alone=r07_suff,
                evidence_level=r07_ev_level,
                evidence={"product_type": product_type, "applicable_products": r07_products},
                message=f"Indicator R07 not applicable to product {product_type}",
            )
        )
    else:
        matched_duplicates: list[dict[str, Any]] = []
        for c in sorted_history:
            if (
                c.policy_id == claim.policy_id
                and c.claim_id != claim.claim_id
                and c.status != "WITHDRAWN"
            ):
                if c.incident_date == claim.incident_date and c.requested_amount > Decimal("0.00"):
                    diff = abs(claim.requested_amount - c.requested_amount)
                    variance_pct = round_money((diff / c.requested_amount) * Decimal("100.00"))
                    if variance_pct <= variance_pct_threshold:
                        matched_duplicates.append(
                            {
                                "claim_ref": c.claim_ref,
                                "incident_date": c.incident_date.isoformat(),
                                "requested_amount": str(c.requested_amount),
                                "variance_percent": str(variance_pct),
                            }
                        )
        r07_triggered = len(matched_duplicates) > 0

        results.append(
            RiskIndicatorResult(
                indicator_id="R07",
                indicator_version=r07_ver,
                triggered=r07_triggered,
                weight_applied=r07_weight if r07_triggered else 0,
                status=(
                    IndicatorEvaluationStatusEnum.TRIGGERED
                    if r07_triggered
                    else IndicatorEvaluationStatusEnum.NOT_TRIGGERED
                ),
                group=r07_group,
                weight=r07_weight,
                sufficient_alone=r07_suff,
                evidence_level=r07_ev_level,
                evidence={
                    "matched_duplicates_count": len(matched_duplicates),
                    "matched_claims": matched_duplicates,
                    "amount_variance_threshold_percent": str(variance_pct_threshold),
                },
                message=(
                    f"Identified {len(matched_duplicates)} existing claim(s) with matching incident date and similar amount"
                    if r07_triggered
                    else "No similar concurrent claims found on policy"
                ),
            )
        )

    # --- R08: Inconsistent document data (Pure attestation / date verification, no PII/name/geo) ---
    cfg_r08 = _get_common_cfg("R08")
    r08_ver = str(cfg_r08.get("version", "1.0"))
    r08_group = str(cfg_r08.get("group", "CONSISTENCY"))
    r08_weight = int(cfg_r08.get("weight", 20))
    r08_enabled = bool(cfg_r08.get("enabled", True))
    r08_suff = bool(cfg_r08.get("sufficient_alone", False))
    r08_ev_level = str(cfg_r08.get("evidence_level", "NONE"))
    r08_products = cfg_r08.get("applicable_products", ["HEALTH", "VEHICLE", "PROPERTY"])

    if not r08_enabled:
        results.append(
            RiskIndicatorResult(
                indicator_id="R08",
                indicator_version=r08_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.DISABLED,
                group=r08_group,
                weight=r08_weight,
                sufficient_alone=r08_suff,
                evidence_level=r08_ev_level,
                evidence={},
                message="Indicator R08 disabled in active ruleset",
            )
        )
    elif product_type not in r08_products:
        results.append(
            RiskIndicatorResult(
                indicator_id="R08",
                indicator_version=r08_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
                group=r08_group,
                weight=r08_weight,
                sufficient_alone=r08_suff,
                evidence_level=r08_ev_level,
                evidence={"product_type": product_type, "applicable_products": r08_products},
                message=f"Indicator R08 not applicable to product {product_type}",
            )
        )
    else:
        inconsistencies: list[dict[str, Any]] = []
        for doc in sorted_docs:
            if doc.status == "VALID":
                # Decision D4: human attestation result MISMATCH (only if current)
                if (
                    doc.identity_attestation_result == "MISMATCH"
                    and doc.is_identity_attestation_current
                ):
                    inconsistencies.append(
                        {
                            "document_id": doc.document_id,
                            "doc_code": doc.doc_code,
                            "type": "IDENTITY_ATTESTATION_MISMATCH",
                        }
                    )
                # Future document date anomaly relative to evaluation date
                if doc.document_date and doc.document_date > eval_date:
                    inconsistencies.append(
                        {
                            "document_id": doc.document_id,
                            "doc_code": doc.doc_code,
                            "type": "FUTURE_DOCUMENT_DATE",
                            "document_date": doc.document_date.isoformat(),
                            "evaluation_date": eval_date.isoformat(),
                        }
                    )

        r08_triggered = len(inconsistencies) > 0
        results.append(
            RiskIndicatorResult(
                indicator_id="R08",
                indicator_version=r08_ver,
                triggered=r08_triggered,
                weight_applied=r08_weight if r08_triggered else 0,
                status=(
                    IndicatorEvaluationStatusEnum.TRIGGERED
                    if r08_triggered
                    else IndicatorEvaluationStatusEnum.NOT_TRIGGERED
                ),
                group=r08_group,
                weight=r08_weight,
                sufficient_alone=r08_suff,
                evidence_level=r08_ev_level,
                evidence={
                    "inconsistencies_count": len(inconsistencies),
                    "details": inconsistencies,
                },
                message=(
                    f"Discovered {len(inconsistencies)} inconsistency issue(s) across claim documents"
                    if r08_triggered
                    else "Document attestation and dates verified consistent"
                ),
            )
        )

    # --- R09: Late filing ---
    cfg_r09 = _get_common_cfg("R09")
    r09_ver = str(cfg_r09.get("version", "1.0"))
    r09_group = str(cfg_r09.get("group", "TIMING"))
    r09_weight = int(cfg_r09.get("weight", 5))
    r09_enabled = bool(cfg_r09.get("enabled", True))
    r09_suff = bool(cfg_r09.get("sufficient_alone", False))
    r09_ev_level = str(cfg_r09.get("evidence_level", "NONE"))
    r09_products = cfg_r09.get("applicable_products", ["HEALTH", "VEHICLE", "PROPERTY"])
    late_filing_window = int(cfg_r09.get("late_filing_window_days", 15))

    if not r09_enabled:
        results.append(
            RiskIndicatorResult(
                indicator_id="R09",
                indicator_version=r09_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.DISABLED,
                group=r09_group,
                weight=r09_weight,
                sufficient_alone=r09_suff,
                evidence_level=r09_ev_level,
                evidence={},
                message="Indicator R09 disabled in active ruleset",
            )
        )
    elif product_type not in r09_products:
        results.append(
            RiskIndicatorResult(
                indicator_id="R09",
                indicator_version=r09_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
                group=r09_group,
                weight=r09_weight,
                sufficient_alone=r09_suff,
                evidence_level=r09_ev_level,
                evidence={"product_type": product_type, "applicable_products": r09_products},
                message=f"Indicator R09 not applicable to product {product_type}",
            )
        )
    else:
        filing_delta_days = (claim.registered_date - claim.incident_date).days
        r09_triggered = filing_delta_days > late_filing_window

        results.append(
            RiskIndicatorResult(
                indicator_id="R09",
                indicator_version=r09_ver,
                triggered=r09_triggered,
                weight_applied=r09_weight if r09_triggered else 0,
                status=(
                    IndicatorEvaluationStatusEnum.TRIGGERED
                    if r09_triggered
                    else IndicatorEvaluationStatusEnum.NOT_TRIGGERED
                ),
                group=r09_group,
                weight=r09_weight,
                sufficient_alone=r09_suff,
                evidence_level=r09_ev_level,
                evidence={
                    "incident_date": claim.incident_date.isoformat(),
                    "registered_date": claim.registered_date.isoformat(),
                    "days_elapsed": filing_delta_days,
                    "threshold_days": late_filing_window,
                },
                message=(
                    f"Claim filed {filing_delta_days} days after incident (standard threshold: {late_filing_window} days)"
                    if r09_triggered
                    else "Claim filed promptly following incident"
                ),
            )
        )

    # --- R10: Round-number amount ---
    cfg_r10 = _get_common_cfg("R10")
    r10_ver = str(cfg_r10.get("version", "1.0"))
    r10_group = str(cfg_r10.get("group", "AMOUNT"))
    r10_weight = int(cfg_r10.get("weight", 5))
    r10_enabled = bool(cfg_r10.get("enabled", True))
    r10_suff = bool(cfg_r10.get("sufficient_alone", False))
    r10_ev_level = str(cfg_r10.get("evidence_level", "NONE"))
    r10_products = cfg_r10.get("applicable_products", ["HEALTH", "VEHICLE", "PROPERTY"])
    round_mult = Decimal(str(cfg_r10.get("round_multiple", 10000)))
    min_round_amt = Decimal(str(cfg_r10.get("min_amount", 50000)))

    if not r10_enabled:
        results.append(
            RiskIndicatorResult(
                indicator_id="R10",
                indicator_version=r10_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.DISABLED,
                group=r10_group,
                weight=r10_weight,
                sufficient_alone=r10_suff,
                evidence_level=r10_ev_level,
                evidence={},
                message="Indicator R10 disabled in active ruleset",
            )
        )
    elif product_type not in r10_products:
        results.append(
            RiskIndicatorResult(
                indicator_id="R10",
                indicator_version=r10_ver,
                triggered=False,
                weight_applied=0,
                status=IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
                group=r10_group,
                weight=r10_weight,
                sufficient_alone=r10_suff,
                evidence_level=r10_ev_level,
                evidence={"product_type": product_type, "applicable_products": r10_products},
                message=f"Indicator R10 not applicable to product {product_type}",
            )
        )
    else:
        r10_triggered = claim.requested_amount >= min_round_amt and (
            claim.requested_amount % round_mult == Decimal("0.00")
        )
        results.append(
            RiskIndicatorResult(
                indicator_id="R10",
                indicator_version=r10_ver,
                triggered=r10_triggered,
                weight_applied=r10_weight if r10_triggered else 0,
                status=(
                    IndicatorEvaluationStatusEnum.TRIGGERED
                    if r10_triggered
                    else IndicatorEvaluationStatusEnum.NOT_TRIGGERED
                ),
                group=r10_group,
                weight=r10_weight,
                sufficient_alone=r10_suff,
                evidence_level=r10_ev_level,
                evidence={
                    "requested_amount": str(claim.requested_amount),
                    "round_multiple": str(round_mult),
                    "min_amount": str(min_round_amt),
                },
                message=(
                    f"Requested amount is an exact multiple of {round_mult} and exceeds {min_round_amt}"
                    if r10_triggered
                    else "Requested amount exhibits standard itemized granularity"
                ),
            )
        )

    # ==========================================
    # 4. Apply group caps
    # ==========================================
    group_scores: dict[str, int] = {}
    for grp_name, grp_cfg in groups_cfg.items():
        grp_cap = int(grp_cfg.get("cap", 100))
        raw_grp_score = sum(
            r.weight_applied
            for r in results
            if r.group == grp_name and r.status == IndicatorEvaluationStatusEnum.TRIGGERED
        )
        group_scores[grp_name] = min(grp_cap, raw_grp_score)

    # 5. Score aggregation: sum of capped groups bounded at 100
    aggregated_score = min(100, max(0, sum(group_scores.values())))

    # ==========================================
    # 6. Completeness calculation
    # ==========================================
    applicable_weight = sum(
        r.weight
        for r in results
        if r.status
        not in (
            IndicatorEvaluationStatusEnum.DISABLED,
            IndicatorEvaluationStatusEnum.NOT_APPLICABLE,
        )
    )
    evaluated_weight = sum(
        r.weight
        for r in results
        if r.status
        in (
            IndicatorEvaluationStatusEnum.TRIGGERED,
            IndicatorEvaluationStatusEnum.NOT_TRIGGERED,
        )
    )

    partial_min_pct = int(thresholds_cfg.get("partial_min_pct", 60))
    mandatory_indicators = thresholds_cfg.get("mandatory_indicators", ["R05", "R06", "R07", "R09"])
    mandatory_evaluated = all(
        r.status
        in (
            IndicatorEvaluationStatusEnum.TRIGGERED,
            IndicatorEvaluationStatusEnum.NOT_TRIGGERED,
        )
        for r in results
        if r.indicator_id in mandatory_indicators
    )

    if applicable_weight == 0:
        completeness = RiskCompletenessEnum.INSUFFICIENT_DATA
    elif evaluated_weight == applicable_weight:
        completeness = RiskCompletenessEnum.COMPLETE
    elif (evaluated_weight * 100 >= partial_min_pct * applicable_weight) and mandatory_evaluated:
        completeness = RiskCompletenessEnum.PARTIAL
    else:
        completeness = RiskCompletenessEnum.INSUFFICIENT_DATA

    # ==========================================
    # 7. Category assignment
    # ==========================================
    low_max = int(thresholds_cfg.get("low_max", 19))
    medium_max = int(thresholds_cfg.get("medium_max", 39))
    investigation_threshold = int(thresholds_cfg.get("investigation_score_threshold", 40))

    if completeness == RiskCompletenessEnum.INSUFFICIENT_DATA:
        category = RiskCategoryEnum.UNASSESSED
        final_score: int | None = None  # CHECK constraint requires NULL score when UNASSESSED
    else:
        final_score = aggregated_score
        if final_score <= low_max:
            category = RiskCategoryEnum.LOW
        elif final_score <= medium_max:
            category = RiskCategoryEnum.MEDIUM
        else:
            category = RiskCategoryEnum.HIGH

    # ==========================================
    # 8. Compute routing & routing reasons
    # ==========================================
    routing_reasons: list[str] = []

    # Any triggered indicator with sufficient_alone
    for r in results:
        if r.triggered and r.sufficient_alone:
            routing_reasons.append(f"SUFFICIENT_ALONE:{r.indicator_id}")

    # Score threshold check (only if score is not None)
    if final_score is not None and final_score >= investigation_threshold:
        routing_reasons.append("SCORE_THRESHOLD")

    requires_investigation = len(routing_reasons) > 0

    # Composed label: "CATEGORY (COMPLETENESS)"
    composed_label = f"{category.value} ({completeness.value})"

    return RiskOutput(
        ruleset_version=ruleset_ver,
        engine_version=engine_ver,
        evaluated_on=eval_date,
        score=final_score,
        category=category,
        completeness=completeness,
        composed_label=composed_label,
        requires_investigation=requires_investigation,
        routing_reasons=tuple(routing_reasons),
        group_scores=group_scores,
        pilot_banner=pilot_banner,
        indicator_results=results,
    )

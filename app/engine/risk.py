"""Pure deterministic risk indicator evaluation engine."""

import re
from decimal import Decimal
from typing import Any

from app.engine.types import (
    DocumentInput,
    HistoricalClaimInput,
    RiskCategoryEnum,
    RiskIndicatorResult,
    RiskInput,
    RiskOutput,
    round_money,
)


def _normalize_name(name: str | None) -> str:
    """Normalize a person's name by stripping non-alphanumeric chars and lowercase."""
    if not name:
        return ""
    return re.sub(r"[^a-zA-Z0-9]", "", name).lower()


def evaluate_risk(risk_input: RiskInput) -> RiskOutput:
    """Evaluate pure deterministic risk indicators R01-R10 and compute aggregated score."""
    ruleset: dict[str, Any] = risk_input.ruleset
    indicators_cfg: dict[str, Any] = ruleset.get("risk_indicators", {})
    thresholds_cfg: dict[str, Any] = ruleset.get("risk_thresholds", {})

    ruleset_ver: str = ruleset.get("ruleset_version", "v1")
    engine_ver: str = ruleset.get("engine_version", "1.0.0")

    claim = risk_input.claim
    policy = risk_input.policy
    coverage = risk_input.coverage
    eval_date = risk_input.evaluation_date

    # Ensure deterministic iteration order across input collections
    sorted_history: list[HistoricalClaimInput] = sorted(
        risk_input.historical_claims, key=lambda c: (c.incident_date, c.claim_id)
    )
    sorted_docs: list[DocumentInput] = sorted(
        risk_input.documents, key=lambda d: d.document_id
    )

    results: list[RiskIndicatorResult] = []

    # ==========================================
    # R01: Repeated claims by customer
    # ==========================================
    cfg_r01 = indicators_cfg.get("R01", {})
    r01_ver = str(cfg_r01.get("version", "1.0"))
    r01_status = str(cfg_r01.get("status", "PILOT"))
    r01_weight = int(cfg_r01.get("weight", 20))
    threshold_claims_r01 = int(cfg_r01.get("threshold_claims", 3))
    window_days_r01 = int(cfg_r01.get("window_days", 365))

    qualifying_cust_claims = [
        c
        for c in sorted_history
        if c.customer_id == claim.customer_id
        and c.claim_id != claim.claim_id
        and c.status != "WITHDRAWN"
        and 0 <= (eval_date - c.registered_date).days <= window_days_r01
    ]
    r01_triggered = len(qualifying_cust_claims) >= threshold_claims_r01
    results.append(
        RiskIndicatorResult(
            indicator_id="R01",
            indicator_version=r01_ver,
            triggered=r01_triggered,
            weight_applied=r01_weight if r01_triggered else 0,
            status=r01_status,
            evidence={
                "prior_qualifying_claims_count": len(qualifying_cust_claims),
                "prior_claim_refs": sorted([c.claim_ref for c in qualifying_cust_claims]),
                "threshold": threshold_claims_r01,
                "window_days": window_days_r01,
            },
            message=(
                f"Customer filed {len(qualifying_cust_claims)} claims in past {window_days_r01} days"
                if r01_triggered
                else "Customer claim frequency within expected parameters"
            ),
        )
    )

    # ==========================================
    # R02: Repeated claims on policy
    # ==========================================
    cfg_r02 = indicators_cfg.get("R02", {})
    r02_ver = str(cfg_r02.get("version", "1.0"))
    r02_status = str(cfg_r02.get("status", "PILOT"))
    r02_weight = int(cfg_r02.get("weight", 15))
    threshold_claims_r02 = int(cfg_r02.get("threshold_claims", 2))
    window_days_r02 = int(cfg_r02.get("window_days", 180))

    qualifying_pol_claims = [
        c
        for c in sorted_history
        if c.policy_id == claim.policy_id
        and c.claim_id != claim.claim_id
        and c.status != "WITHDRAWN"
        and 0 <= (eval_date - c.registered_date).days <= window_days_r02
    ]
    r02_triggered = len(qualifying_pol_claims) >= threshold_claims_r02
    results.append(
        RiskIndicatorResult(
            indicator_id="R02",
            indicator_version=r02_ver,
            triggered=r02_triggered,
            weight_applied=r02_weight if r02_triggered else 0,
            status=r02_status,
            evidence={
                "policy_claims_count": len(qualifying_pol_claims),
                "prior_claim_refs": sorted([c.claim_ref for c in qualifying_pol_claims]),
                "threshold": threshold_claims_r02,
                "window_days": window_days_r02,
            },
            message=(
                f"Policy has {len(qualifying_pol_claims)} claims in past {window_days_r02} days"
                if r02_triggered
                else "Policy claim frequency within expected parameters"
            ),
        )
    )

    # ==========================================
    # R03: High amount vs limit
    # ==========================================
    cfg_r03 = indicators_cfg.get("R03", {})
    r03_ver = str(cfg_r03.get("version", "1.0"))
    r03_status = str(cfg_r03.get("status", "PILOT"))
    r03_weight = int(cfg_r03.get("weight", 20))
    limit_ratio_threshold = Decimal(str(cfg_r03.get("limit_ratio_threshold_percent", 80)))

    if coverage is None:
        results.append(
            RiskIndicatorResult(
                indicator_id="R03",
                indicator_version=r03_ver,
                triggered=False,
                weight_applied=0,
                status=r03_status,
                skipped=True,
                skip_reason="Coverage details not available",
                evidence={},
                message="Skipped due to missing coverage details",
            )
        )
    else:
        rem_cap = coverage.limit_amount - coverage.consumed_amount
        rem_limit = round_money(max(Decimal("0.00"), rem_cap))
        threshold_amt = round_money((rem_limit * limit_ratio_threshold) / Decimal("100.00"))
        if rem_limit <= Decimal("0.00"):
            r03_triggered = claim.requested_amount > Decimal("0.00")
            ratio_pct = Decimal("100.00") if r03_triggered else Decimal("0.00")
        else:
            ratio_pct = round_money((claim.requested_amount / rem_limit) * Decimal("100.00"))
            r03_triggered = claim.requested_amount > threshold_amt

        results.append(
            RiskIndicatorResult(
                indicator_id="R03",
                indicator_version=r03_ver,
                triggered=r03_triggered,
                weight_applied=r03_weight if r03_triggered else 0,
                status=r03_status,
                evidence={
                    "requested_amount": str(claim.requested_amount),
                    "remaining_limit": str(rem_limit),
                    "threshold_amount": str(threshold_amt),
                    "ratio_percent": str(ratio_pct),
                    "threshold_percent": str(limit_ratio_threshold),
                },
                message=(
                    f"Requested amount represents {ratio_pct}% of remaining limit (threshold: {limit_ratio_threshold}%)"
                    if r03_triggered
                    else "Requested amount within standard limit proportions"
                ),
            )
        )

    # ==========================================
    # R04: Unusual amount vs peers
    # ==========================================
    cfg_r04 = indicators_cfg.get("R04", {})
    r04_ver = str(cfg_r04.get("version", "1.0"))
    r04_status = str(cfg_r04.get("status", "PILOT"))
    r04_weight = int(cfg_r04.get("weight", 15))
    peer_mult = Decimal(str(cfg_r04.get("peer_multiple_threshold", 3)))
    min_peer_claims = int(cfg_r04.get("min_historical_claims", 10))

    peer_amounts = sorted(risk_input.peer_approved_amounts)
    if len(peer_amounts) < min_peer_claims:
        results.append(
            RiskIndicatorResult(
                indicator_id="R04",
                indicator_version=r04_ver,
                triggered=False,
                weight_applied=0,
                status=r04_status,
                skipped=True,
                skip_reason=f"Insufficient peer claims ({len(peer_amounts)} < {min_peer_claims})",
                evidence={"available_peer_claims": len(peer_amounts), "required": min_peer_claims},
                message=f"Skipped due to insufficient peer baseline data ({len(peer_amounts)} historical claims)",
            )
        )
    else:
        n = len(peer_amounts)
        if n % 2 == 1:
            median_amt = peer_amounts[n // 2]
        else:
            median_amt = round_money((peer_amounts[n // 2 - 1] + peer_amounts[n // 2]) / Decimal("2"))

        threshold_peer_amt = round_money(median_amt * peer_mult)
        r04_triggered = claim.requested_amount > threshold_peer_amt
        results.append(
            RiskIndicatorResult(
                indicator_id="R04",
                indicator_version=r04_ver,
                triggered=r04_triggered,
                weight_applied=r04_weight if r04_triggered else 0,
                status=r04_status,
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

    # ==========================================
    # R05: Early claim
    # ==========================================
    cfg_r05 = indicators_cfg.get("R05", {})
    r05_ver = str(cfg_r05.get("version", "1.0"))
    r05_status = str(cfg_r05.get("status", "PILOT"))
    r05_weight = int(cfg_r05.get("weight", 25))
    early_window = int(cfg_r05.get("early_claim_window_days", 30))

    if policy is None:
        results.append(
            RiskIndicatorResult(
                indicator_id="R05",
                indicator_version=r05_ver,
                triggered=False,
                weight_applied=0,
                status=r05_status,
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
                status=r05_status,
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

    # ==========================================
    # R06: Late-in-policy claim
    # ==========================================
    cfg_r06 = indicators_cfg.get("R06", {})
    r06_ver = str(cfg_r06.get("version", "1.0"))
    r06_status = str(cfg_r06.get("status", "PILOT"))
    r06_weight = int(cfg_r06.get("weight", 10))
    late_window = int(cfg_r06.get("late_policy_window_days", 7))

    if policy is None:
        results.append(
            RiskIndicatorResult(
                indicator_id="R06",
                indicator_version=r06_ver,
                triggered=False,
                weight_applied=0,
                status=r06_status,
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
                status=r06_status,
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

    # ==========================================
    # R07: Duplicate suspicion
    # ==========================================
    cfg_r07 = indicators_cfg.get("R07", {})
    r07_ver = str(cfg_r07.get("version", "1.0"))
    r07_status = str(cfg_r07.get("status", "PILOT"))
    r07_weight = int(cfg_r07.get("weight", 30))
    variance_pct_threshold = Decimal(str(cfg_r07.get("amount_variance_percent", 5)))

    matched_duplicates: list[dict[str, Any]] = []
    for c in sorted_history:
        if c.policy_id == claim.policy_id and c.claim_id != claim.claim_id and c.status != "WITHDRAWN":
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
            status=r07_status,
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

    # ==========================================
    # R08: Inconsistent document data
    # ==========================================
    cfg_r08 = indicators_cfg.get("R08", {})
    r08_ver = str(cfg_r08.get("version", "1.0"))
    r08_status = str(cfg_r08.get("status", "PILOT"))
    r08_weight = int(cfg_r08.get("weight", 20))

    norm_cust_name = _normalize_name(risk_input.customer_name)
    inconsistencies: list[dict[str, Any]] = []

    for doc in sorted_docs:
        if doc.status == "VALID":
            # Check named_person consistency
            if doc.named_person:
                norm_doc_name = _normalize_name(doc.named_person)
                if norm_doc_name and norm_cust_name and norm_doc_name != norm_cust_name:
                    inconsistencies.append(
                        {
                            "document_id": doc.document_id,
                            "doc_code": doc.doc_code,
                            "type": "NAME_MISMATCH",
                            "document_named_person": doc.named_person,
                            "customer_name": risk_input.customer_name,
                        }
                    )

            # Check pre-incident date anomalies (e.g. document date in future relative to eval_date)
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
            status=r08_status,
            evidence={"inconsistencies_count": len(inconsistencies), "details": inconsistencies},
            message=(
                f"Discovered {len(inconsistencies)} inconsistency issue(s) across claim documents"
                if r08_triggered
                else "Document data consistent with customer record"
            ),
        )
    )

    # ==========================================
    # R09: Late filing
    # ==========================================
    cfg_r09 = indicators_cfg.get("R09", {})
    r09_ver = str(cfg_r09.get("version", "1.0"))
    r09_status = str(cfg_r09.get("status", "PILOT"))
    r09_weight = int(cfg_r09.get("weight", 5))
    late_filing_window = int(cfg_r09.get("late_filing_window_days", 15))

    filing_delta_days = (claim.registered_date - claim.incident_date).days
    r09_triggered = filing_delta_days > late_filing_window
    results.append(
        RiskIndicatorResult(
            indicator_id="R09",
            indicator_version=r09_ver,
            triggered=r09_triggered,
            weight_applied=r09_weight if r09_triggered else 0,
            status=r09_status,
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

    # ==========================================
    # R10: Round-number amount
    # ==========================================
    cfg_r10 = indicators_cfg.get("R10", {})
    r10_ver = str(cfg_r10.get("version", "1.0"))
    r10_status = str(cfg_r10.get("status", "PILOT"))
    r10_weight = int(cfg_r10.get("weight", 5))
    round_mult = Decimal(str(cfg_r10.get("round_multiple", 10000)))
    min_round_amt = Decimal(str(cfg_r10.get("min_amount", 50000)))

    r10_triggered = (
        claim.requested_amount >= min_round_amt
        and (claim.requested_amount % round_mult == Decimal("0.00"))
    )
    results.append(
        RiskIndicatorResult(
            indicator_id="R10",
            indicator_version=r10_ver,
            triggered=r10_triggered,
            weight_applied=r10_weight if r10_triggered else 0,
            status=r10_status,
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
    # Score Aggregation and Categorization
    # ==========================================
    raw_score = sum(r.weight_applied for r in results if r.triggered)
    final_score = min(100, max(0, raw_score))

    investigation_threshold = int(thresholds_cfg.get("investigation_score_threshold", 25))
    low_max = int(thresholds_cfg.get("low_max", 24))
    medium_max = int(thresholds_cfg.get("medium_max", 49))

    if final_score <= low_max:
        category = RiskCategoryEnum.LOW
    elif final_score <= medium_max:
        category = RiskCategoryEnum.MEDIUM
    else:
        category = RiskCategoryEnum.HIGH

    requires_investigation = final_score >= investigation_threshold

    return RiskOutput(
        ruleset_version=ruleset_ver,
        engine_version=engine_ver,
        evaluated_on=eval_date,
        score=final_score,
        category=category,
        requires_investigation=requires_investigation,
        indicator_results=results,
    )

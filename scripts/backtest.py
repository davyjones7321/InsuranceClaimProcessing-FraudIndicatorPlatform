"""Risk governance and indicator backtest report generator (P6 Task 12).

Reports overall and per-indicator flag rates, co-occurrence, stability by month
and product, threshold sensitivity, expected investigator workload, and concern
rates among investigated claims with mandatory selection-bias caveat.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Claim, Investigation, Policy, RiskEvaluation
from app.db.session import SessionLocal
from app.domain.enums import InvestigationOutcome

FIXED_DISCLAIMER = (
    "DISCLAIMER: This operational report evaluates indicator flag distributions and "
    "investigator workload. It does NOT report ground-truth performance metrics "
    "against unobserved true states. Verification bias applies: uninvestigated claims "
    "have unverified outcomes. Observed concern rates among investigated claims reflect "
    "referral selection and cannot be generalized to the uninvestigated population."
)

SELECTION_BIAS_CAVEAT = (
    "Selection bias caveat: Concern rates among investigated claims cannot be "
    "generalized to unflagged or uninvestigated claims due to verification bias."
)


@dataclass
class IndicatorStats:
    indicator_id: str
    triggered_count: int = 0
    flag_rate: float = 0.0


@dataclass
class MonthlyStability:
    year_month: str
    total_count: int = 0
    flagged_count: int = 0
    flag_rate: float = 0.0


@dataclass
class ProductStability:
    product_type: str
    total_count: int = 0
    flagged_count: int = 0
    flag_rate: float = 0.0


@dataclass
class ThresholdSensitivity:
    threshold: int
    flagged_count: int = 0
    flag_rate: float = 0.0


@dataclass
class InvestigationConcernStats:
    cohort: str
    total_investigated: int = 0
    cleared_count: int = 0
    inconclusive_count: int = 0
    concerns_confirmed_count: int = 0
    concern_rate: float = 0.0


@dataclass
class BacktestReport:
    disclaimer: str = FIXED_DISCLAIMER
    selection_bias_caveat: str = SELECTION_BIAS_CAVEAT
    total_evaluations: int = 0
    total_flagged: int = 0
    overall_flag_rate: float = 0.0
    total_unassessed: int = 0
    indicator_stats: list[IndicatorStats] = field(default_factory=list)
    co_occurrence_matrix: dict[str, dict[str, int]] = field(default_factory=dict)
    monthly_stability: list[MonthlyStability] = field(default_factory=list)
    product_stability: list[ProductStability] = field(default_factory=list)
    threshold_sensitivity: list[ThresholdSensitivity] = field(default_factory=list)
    investigator_workload: dict[str, Any] = field(default_factory=dict)
    investigation_concerns: list[InvestigationConcernStats] = field(default_factory=list)
    segment_disparity: dict[str, Any] = field(default_factory=dict)


def run_backtest(
    db: Session,
    product_filter: str | None = None,
    evaluation_date_from: date | None = None,
    evaluation_date_to: date | None = None,
) -> BacktestReport:
    """Compute risk indicator distributions and backtest statistics across recorded claims."""
    stmt = (
        select(RiskEvaluation, Claim, Policy)
        .join(Claim, RiskEvaluation.claim_id == Claim.id)
        .join(Policy, Claim.policy_id == Policy.id)
    )
    if product_filter:
        stmt = stmt.where(Policy.product_type == product_filter)
    if evaluation_date_from:
        stmt = stmt.where(RiskEvaluation.evaluated_on >= evaluation_date_from)
    if evaluation_date_to:
        stmt = stmt.where(RiskEvaluation.evaluated_on <= evaluation_date_to)

    rows = db.execute(stmt).all()

    report = BacktestReport()
    report.total_evaluations = len(rows)
    if not rows:
        return report

    all_indicators = [f"R{i:02d}" for i in range(1, 11)]
    indicator_counts: dict[str, int] = dict.fromkeys(all_indicators, 0)
    co_occur: dict[str, dict[str, int]] = {
        ind: dict.fromkeys(all_indicators, 0) for ind in all_indicators
    }

    monthly_map: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "flagged": 0})
    product_map: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "flagged": 0})
    thresholds = [20, 30, 40, 50, 60, 70]
    thresh_counts: dict[int, int] = dict.fromkeys(thresholds, 0)

    claim_ids_flagged: set[Any] = set()
    claim_ids_evaluated: set[Any] = set()

    for eval_run, claim, policy in rows:
        claim_ids_evaluated.add(claim.id)
        ym = eval_run.evaluated_on.strftime("%Y-%m")
        prod = policy.product_type

        monthly_map[ym]["total"] += 1
        product_map[prod]["total"] += 1

        is_flagged = bool(eval_run.requires_investigation)
        if is_flagged:
            report.total_flagged += 1
            monthly_map[ym]["flagged"] += 1
            product_map[prod]["flagged"] += 1
            claim_ids_flagged.add(claim.id)

        if eval_run.completeness == "INSUFFICIENT_DATA" or eval_run.category == "UNASSESSED":
            report.total_unassessed += 1

        # Score thresholds
        score_val = eval_run.score or 0
        for t in thresholds:
            if score_val >= t or is_flagged:
                thresh_counts[t] += 1

        # Indicators triggered
        triggered_in_run: list[str] = []
        for ind_res in eval_run.indicator_results:
            if ind_res.triggered:
                ind_id = ind_res.indicator_id
                if ind_id in indicator_counts:
                    indicator_counts[ind_id] += 1
                    triggered_in_run.append(ind_id)

        # Co-occurrence
        for i_a in triggered_in_run:
            for i_b in triggered_in_run:
                if i_a != i_b:
                    co_occur[i_a][i_b] += 1

    report.overall_flag_rate = round(report.total_flagged / report.total_evaluations, 4)

    # Indicator stats
    for ind in all_indicators:
        cnt = indicator_counts[ind]
        rate = round(cnt / report.total_evaluations, 4)
        report.indicator_stats.append(
            IndicatorStats(indicator_id=ind, triggered_count=cnt, flag_rate=rate)
        )
    report.co_occurrence_matrix = co_occur

    # Monthly stability
    for ym in sorted(monthly_map.keys()):
        m_tot = monthly_map[ym]["total"]
        m_flg = monthly_map[ym]["flagged"]
        report.monthly_stability.append(
            MonthlyStability(
                year_month=ym,
                total_count=m_tot,
                flagged_count=m_flg,
                flag_rate=round(m_flg / m_tot, 4) if m_tot > 0 else 0.0,
            )
        )

    # Product stability
    for prod in sorted(product_map.keys()):
        p_tot = product_map[prod]["total"]
        p_flg = product_map[prod]["flagged"]
        report.product_stability.append(
            ProductStability(
                product_type=prod,
                total_count=p_tot,
                flagged_count=p_flg,
                flag_rate=round(p_flg / p_tot, 4) if p_tot > 0 else 0.0,
            )
        )

    # Threshold sensitivity
    for t in thresholds:
        t_cnt = thresh_counts[t]
        report.threshold_sensitivity.append(
            ThresholdSensitivity(
                threshold=t,
                flagged_count=t_cnt,
                flag_rate=round(t_cnt / report.total_evaluations, 4),
            )
        )

    # Expected investigator workload
    months_count = max(1, len(monthly_map))
    avg_per_month = round(report.total_flagged / months_count, 1)
    avg_per_week = round(avg_per_month / 4.33, 1)
    report.investigator_workload = {
        "total_flagged_claims": report.total_flagged,
        "observed_months": months_count,
        "estimated_claims_per_month": avg_per_month,
        "estimated_claims_per_week": avg_per_week,
    }

    # Investigation concerns among investigated claims
    investigations = db.scalars(
        select(Investigation).where(Investigation.claim_id.in_(claim_ids_evaluated))
    ).all()

    flagged_inv = [inv for inv in investigations if inv.claim_id in claim_ids_flagged]
    unflagged_inv = [inv for inv in investigations if inv.claim_id not in claim_ids_flagged]

    def _summarize_investigations(
        cohort_name: str, inv_list: list[Investigation]
    ) -> InvestigationConcernStats:
        total = len(inv_list)
        cleared = sum(1 for inv in inv_list if inv.outcome == InvestigationOutcome.CLEARED)
        inconc = sum(1 for inv in inv_list if inv.outcome == InvestigationOutcome.INCONCLUSIVE)
        confirmed = sum(
            1 for inv in inv_list if inv.outcome == InvestigationOutcome.CONCERNS_CONFIRMED
        )
        rate = round(confirmed / total, 4) if total > 0 else 0.0
        return InvestigationConcernStats(
            cohort=cohort_name,
            total_investigated=total,
            cleared_count=cleared,
            inconclusive_count=inconc,
            concerns_confirmed_count=confirmed,
            concern_rate=rate,
        )

    report.investigation_concerns = [
        _summarize_investigations("Flagged (Referred via Engine)", flagged_inv),
        _summarize_investigations("Unflagged (Referred Manually)", unflagged_inv),
    ]

    # Segment disparity screen
    segment_disparity: dict[str, Any] = {}
    for prod in product_map:
        p_tot = product_map[prod]["total"]
        p_flg = product_map[prod]["flagged"]
        p_rate = p_flg / p_tot if p_tot > 0 else 0.0
        rel_ratio = (
            round(p_rate / report.overall_flag_rate, 2) if report.overall_flag_rate > 0 else 1.0
        )
        segment_disparity[prod] = {
            "evaluations": p_tot,
            "flagged": p_flg,
            "flag_rate": round(p_rate, 4),
            "rate_ratio_vs_overall": rel_ratio,
        }
    report.segment_disparity = segment_disparity

    return report


def format_text_report(report: BacktestReport) -> str:
    """Format report as structured plain text with fixed disclaimer."""
    lines: list[str] = [
        "=" * 80,
        "RISK GOVERNANCE AND INDICATOR BACKTEST REPORT",
        "=" * 80,
        report.disclaimer,
        "=" * 80,
        "",
        "1. EXECUTIVE SUMMARY",
        f"   Total evaluations:            {report.total_evaluations}",
        f"   Total flagged claims:         {report.total_flagged}",
        f"   Overall flag rate:            {report.overall_flag_rate * 100:.2f}%",
        f"   Total unassessed (incomplete): {report.total_unassessed}",
        "",
        "2. PER-INDICATOR FLAG RATES",
        "   Indicator | Triggered Count | Flag Rate (%)",
        "   --------- | --------------- | -------------",
    ]
    for ind in report.indicator_stats:
        lines.append(
            f"   {ind.indicator_id:<9} | {ind.triggered_count:<15} | {ind.flag_rate * 100:>11.2f}%"
        )

    lines.extend(
        [
            "",
            "3. STABILITY BY MONTH",
            "   Month   | Evaluations | Flagged | Flag Rate (%)",
            "   ------- | ----------- | ------- | -------------",
        ]
    )
    for m in report.monthly_stability:
        lines.append(
            f"   {m.year_month:<7} | {m.total_count:<11} | {m.flagged_count:<7} | {m.flag_rate * 100:>11.2f}%"
        )

    lines.extend(
        [
            "",
            "4. STABILITY BY PRODUCT",
            "   Product   | Evaluations | Flagged | Flag Rate (%)",
            "   --------- | ----------- | ------- | -------------",
        ]
    )
    for p in report.product_stability:
        lines.append(
            f"   {p.product_type:<9} | {p.total_count:<11} | {p.flagged_count:<7} | {p.flag_rate * 100:>11.2f}%"
        )

    lines.extend(
        [
            "",
            "5. THRESHOLD SENSITIVITY",
            "   Threshold | Flagged Count | Flag Rate (%)",
            "   --------- | ------------- | -------------",
        ]
    )
    for t in report.threshold_sensitivity:
        lines.append(
            f"   Score>={t.threshold:<3} | {t.flagged_count:<13} | {t.flag_rate * 100:>11.2f}%"
        )

    lines.extend(
        [
            "",
            "6. ESTIMATED INVESTIGATOR WORKLOAD",
            f"   Total flagged claims:         {report.investigator_workload.get('total_flagged_claims', 0)}",
            f"   Estimated claims / month:     {report.investigator_workload.get('estimated_claims_per_month', 0.0)}",
            f"   Estimated claims / week:      {report.investigator_workload.get('estimated_claims_per_week', 0.0)}",
            "",
            "7. CONCERN RATES AMONG INVESTIGATED CLAIMS",
            f"   {report.selection_bias_caveat}",
            "   Cohort                       | Investigated | Cleared | Inconclusive | Confirmed | Concern Rate (%)",
            "   ---------------------------- | ------------ | ------- | ------------ | --------- | ----------------",
        ]
    )
    for c in report.investigation_concerns:
        lines.append(
            f"   {c.cohort:<28} | {c.total_investigated:<12} | {c.cleared_count:<7} | {c.inconclusive_count:<12} | {c.concerns_confirmed_count:<9} | {c.concern_rate * 100:>14.2f}%"
        )

    lines.extend(
        [
            "",
            "8. SEGMENT DISPARITY SCREEN",
            "   Segment   | Evaluations | Flagged | Flag Rate (%) | Rate Ratio vs Overall",
            "   --------- | ----------- | ------- | ------------- | ---------------------",
        ]
    )
    for seg, data in report.segment_disparity.items():
        lines.append(
            f"   {seg:<9} | {data['evaluations']:<11} | {data['flagged']:<7} | {data['flag_rate'] * 100:>11.2f}% | {data['rate_ratio_vs_overall']:>21.2f}x"
        )
    lines.append("=" * 80)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate risk governance indicator distribution and workload backtest report."
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--product",
        type=str,
        default=None,
        help="Filter evaluation backtest by product type",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = run_backtest(db=db, product_filter=args.product)
        if args.format == "json":
            print(json.dumps(asdict(report), indent=2, default=str))
        else:
            print(format_text_report(report))
    finally:
        db.close()


if __name__ == "__main__":
    main()

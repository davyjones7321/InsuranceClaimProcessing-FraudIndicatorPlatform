"""A+ sample review statistical fit report generator (Decision D13, P5 Task 12).

Reads reviewer CSV of closed claims and reports shares by count and by settled value
per product with Wilson 95% score confidence intervals and PASS/FAIL evaluation against D13 thresholds.
"""

import argparse
import csv
import json
import math
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

# 95% two-sided normal quantile
Z_95 = 1.95996398454


def wilson_score_interval(
    successes: int, trials: int, confidence_z: float = Z_95
) -> tuple[float, float]:
    """Compute Wilson score 95% confidence interval for a proportion."""
    if trials == 0:
        return 0.0, 0.0
    p = successes / trials
    denom = 1.0 + (confidence_z**2) / trials
    centre = (p + (confidence_z**2) / (2.0 * trials)) / denom
    margin = (
        confidence_z
        * math.sqrt((p * (1.0 - p) / trials) + ((confidence_z**2) / (4.0 * (trials**2))))
    ) / denom
    lower = max(0.0, centre - margin)
    upper = min(1.0, centre + margin)
    return lower, upper


def evaluate_aplus_csv(csv_path: Path) -> dict[str, Any]:
    """Analyze completed A+ sample review CSV and calculate stats per product."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Review CSV not found: {csv_path}")

    reader = csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines())
    products: dict[str, dict[str, Any]] = {}

    for _row_idx, row in enumerate(reader, start=1):
        product = row["product"].strip().upper()
        if product not in products:
            products[product] = {
                "total_count": 0,
                "fit_count": 0,
                "splittable_count": 0,
                "nofit_count": 0,
                "cross_coverage_nofit_count": 0,
                "total_value": Decimal("0.00"),
                "fit_value": Decimal("0.00"),
                "splittable_value": Decimal("0.00"),
                "nofit_value": Decimal("0.00"),
            }

        stats = products[product]
        stats["total_count"] += 1

        val_str = row.get("settled_amount", "0.00").strip() or "0.00"
        value = Decimal(val_str)
        stats["total_value"] += value

        # Use adjudicated_result if present, otherwise classification
        classification = (
            row.get("adjudicated_result", "").strip().upper()
            or row.get("classification", "").strip().upper()
        )
        reason_code = row.get("reason_code", "").strip().upper()

        if classification == "FIT":
            stats["fit_count"] += 1
            stats["fit_value"] += value
        elif classification == "SPLITTABLE":
            stats["splittable_count"] += 1
            stats["splittable_value"] += value
        else:  # NO-FIT
            stats["nofit_count"] += 1
            stats["nofit_value"] += value
            if reason_code in (
                "CROSS_COVERAGE_JOINT_CALC",
                "SHARED_AGGREGATE_DEDUCTIBLE",
                "POLICY_AGGREGATE_LIMIT",
            ):
                stats["cross_coverage_nofit_count"] += 1

    overall_pass = True
    results: dict[str, Any] = {}

    for prod, s in products.items():
        total_c = s["total_count"]
        supported_c = s["fit_count"] + s["splittable_count"]
        count_share = (supported_c / total_c) if total_c > 0 else 0.0
        count_ci_low, count_ci_high = wilson_score_interval(supported_c, total_c)

        total_v = s["total_value"]
        supported_v = s["fit_value"] + s["splittable_value"]
        value_share = float(supported_v / total_v) if total_v > Decimal("0.00") else 0.0

        cross_c = s["cross_coverage_nofit_count"]
        cross_share = (cross_c / total_c) if total_c > 0 else 0.0

        # Thresholds from Decision D13:
        # count_share >= 90%, value_share >= 90%, cross_share <= 10%
        count_pass = count_share >= 0.90
        value_pass = value_share >= 0.90
        cross_pass = cross_share <= 0.10
        prod_pass = count_pass and value_pass and cross_pass

        if not prod_pass:
            overall_pass = False

        results[prod] = {
            "status": "PASS" if prod_pass else "FAIL",
            "total_count": total_c,
            "fit_count": s["fit_count"],
            "splittable_count": s["splittable_count"],
            "nofit_count": s["nofit_count"],
            "count_share_pct": round(count_share * 100, 2),
            "count_ci_95_pct": [round(count_ci_low * 100, 2), round(count_ci_high * 100, 2)],
            "total_value": str(total_v),
            "supported_value": str(supported_v),
            "value_share_pct": round(value_share * 100, 2),
            "cross_coverage_share_pct": round(cross_share * 100, 2),
            "threshold_evaluations": {
                "count_gte_90_pct": count_pass,
                "value_gte_90_pct": value_pass,
                "cross_coverage_lte_10_pct": cross_pass,
            },
        }

    return {
        "overall_status": "PASS" if overall_pass else "FAIL",
        "products": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="A+ Architecture Sample Review Fit Report")
    parser.add_argument(
        "--csv",
        "-c",
        type=str,
        default="data/sample_review_template.csv",
        help="Path to completed sample review CSV file",
    )
    parser.add_argument(
        "--json-output",
        "-j",
        type=str,
        default=None,
        help="Optional path to export JSON results",
    )
    args = parser.parse_args()

    report = evaluate_aplus_csv(Path(args.csv))

    print("\n================================================================================")
    print("                     A+ SAMPLE REVIEW STATISTICAL FIT REPORT                     ")
    print(f"Overall Acceptance Status: {report['overall_status']}")
    print("================================================================================")

    for prod, data in report["products"].items():
        print(f"\nProduct: {prod} -> [{data['status']}]")
        print(
            f"  Sample Claims Count:   {data['total_count']} (FIT: {data['fit_count']}, SPLITTABLE: {data['splittable_count']}, NO-FIT: {data['nofit_count']})"
        )
        print(f"  Count Share (A+ Fit):  {data['count_share_pct']}% (Target: >= 90.0%)")
        print(
            f"  Wilson 95% Conf Int:   [{data['count_ci_95_pct'][0]}%, {data['count_ci_95_pct'][1]}%]"
        )
        print(f"  Value Share (A+ Fit):  {data['value_share_pct']}% (Target: >= 90.0%)")
        print(f"  Cross-Coverage NO-FIT: {data['cross_coverage_share_pct']}% (Target: <= 10.0%)")

    print("\n================================================================================")

    if args.json_output:
        Path(args.json_output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Exported JSON report to: {args.json_output}")

    sys.exit(0 if report["overall_status"] == "PASS" else 1)


if __name__ == "__main__":
    main()

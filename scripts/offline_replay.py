"""Offline replay runner executing pure engine against sealed bundles (P5 Task 11).

Runs completely offline without database connections, secrets, or service runtimes.
Imports only `app.engine` pure logic to verify bit-for-bit output match.
"""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.engine.assessment_cap import calculate_assessment_cap
from app.engine.risk import evaluate_risk
from app.engine.validation import evaluate_validation
from app.schemas.snapshots import (
    compute_ruleset_hash,
    deserialize_assessment_snapshot,
    deserialize_risk_snapshot,
    deserialize_validation_snapshot,
)


def replay_offline_bundle(bundle_data: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    """Execute offline replay against sealed bundle data containing snapshots and expected outputs."""
    report: dict[str, Any] = {
        "status": "PASSED",
        "validation_runs_checked": 0,
        "risk_evaluations_checked": 0,
        "assessments_checked": 0,
        "failures": [],
    }

    ruleset = bundle_data.get("ruleset", {})
    report["ruleset_hash"] = compute_ruleset_hash(ruleset) if ruleset else None

    # 1. Validation runs
    for run in bundle_data.get("validation_runs", []):
        report["validation_runs_checked"] += 1
        run_id = run.get("id", "unknown")
        try:
            val_input = deserialize_validation_snapshot(run["inputs_snapshot"], ruleset=ruleset)
            replayed = evaluate_validation(val_input)
            expected = run.get("expected_output", {})

            if expected and replayed.overall_result.value != expected.get("overall_result"):
                msg = (
                    f"ValidationRun {run_id} overall_result mismatch: "
                    f"expected={expected.get('overall_result')}, got={replayed.overall_result.value}"
                )
                report["failures"].append(msg)
                if verbose:
                    print(f"[FAIL] {msg}")
            elif verbose:
                print(f"[OK] ValidationRun {run_id} matched {replayed.overall_result.value}")
        except Exception as exc:
            msg = f"ValidationRun {run_id} crashed: {exc}"
            report["failures"].append(msg)
            if verbose:
                print(f"[FAIL] {msg}")

    # 2. Risk evaluations
    for r_eval in bundle_data.get("risk_evaluations", []):
        report["risk_evaluations_checked"] += 1
        eval_id = r_eval.get("id", "unknown")
        try:
            risk_input = deserialize_risk_snapshot(r_eval["inputs_snapshot"], ruleset=ruleset)
            replayed = evaluate_risk(risk_input)
            expected = r_eval.get("expected_output", {})

            if expected and replayed.score != expected.get("score"):
                msg = f"RiskEvaluation {eval_id} score mismatch: expected={expected.get('score')}, got={replayed.score}"
                report["failures"].append(msg)
            elif expected and replayed.category.value != expected.get("category"):
                msg = (
                    f"RiskEvaluation {eval_id} category mismatch: "
                    f"expected={expected.get('category')}, got={replayed.category.value}"
                )
                report["failures"].append(msg)
            elif verbose:
                print(f"[OK] RiskEvaluation {eval_id} matched score={replayed.score}")
        except Exception as exc:
            msg = f"RiskEvaluation {eval_id} crashed: {exc}"
            report["failures"].append(msg)

    # 3. Assessments
    for a_rec in bundle_data.get("assessments", []):
        report["assessments_checked"] += 1
        a_id = a_rec.get("id", "unknown")
        try:
            inp = deserialize_assessment_snapshot(a_rec["inputs_snapshot"])
            out = calculate_assessment_cap(inp)
            expected = a_rec.get("expected_output", {})

            expected_cap = Decimal(str(expected.get("max_eligible_amount"))) if expected else None
            if expected_cap is not None and out.max_eligible_amount != expected_cap:
                msg = (
                    f"Assessment {a_id} cap mismatch: "
                    f"expected={expected_cap}, got={out.max_eligible_amount}"
                )
                report["failures"].append(msg)
            elif verbose:
                print(f"[OK] Assessment {a_id} matched max_eligible={out.max_eligible_amount}")
        except Exception as exc:
            msg = f"Assessment {a_id} crashed: {exc}"
            report["failures"].append(msg)

    if report["failures"]:
        report["status"] = "FAILED"

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Deterministic Engine Replay Runner")
    parser.add_argument(
        "--bundle",
        "-b",
        type=str,
        required=True,
        help="Path to sealed bundle JSON file containing snapshots and ruleset",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Optional path to write comparison report JSON",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed output",
    )
    args = parser.parse_args()

    bundle_path = Path(args.bundle)
    if not bundle_path.exists():
        print(f"Error: Bundle file '{bundle_path}' not found", file=sys.stderr)
        sys.exit(1)

    bundle_data = json.loads(bundle_path.read_text(encoding="utf-8"))
    report = replay_offline_bundle(bundle_data, verbose=args.verbose)

    print("\n--- Offline Replay Report ---")
    print(f"Status:                      {report['status']}")
    print(f"Validation runs checked:     {report['validation_runs_checked']}")
    print(f"Risk evaluations checked:    {report['risk_evaluations_checked']}")
    print(f"Assessments checked:         {report['assessments_checked']}")
    print(f"Total failures:              {len(report['failures'])}")

    if report["failures"]:
        print("\nFailures:")
        for f in report["failures"]:
            print(f"  - {f}")

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written to: {args.output}")

    sys.exit(0 if report["status"] == "PASSED" else 1)


if __name__ == "__main__":
    main()

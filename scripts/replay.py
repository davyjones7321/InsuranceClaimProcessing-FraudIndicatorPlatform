"""Deterministic engine replay CLI script.

Verifies pure engine determinism by replaying validation runs and risk evaluations
against recorded database states and asserting exact matching outcomes, scores, and reason codes.
"""

import argparse
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Assessment, Claim, Coverage, RiskEvaluation, ValidationRun
from app.db.session import SessionLocal
from app.engine.assessment_cap import calculate_assessment_cap
from app.engine.risk import evaluate_risk
from app.engine.types import AssessmentInput
from app.engine.validation import evaluate_validation
from app.services.risk_service import RiskService
from app.services.validation_service import ValidationService


def replay_validation_run(
    db: Session, run: ValidationRun, verbose: bool = False
) -> tuple[bool, list[str]]:
    """Replay a single stored validation run and verify bit-for-bit determinism."""
    claim = db.get(Claim, run.claim_id)
    if not claim:
        return False, [f"Claim '{run.claim_id}' not found for ValidationRun '{run.id}'"]

    service = ValidationService(db)
    val_input = service.build_validation_input(
        claim=claim,
        evaluation_date=run.evaluated_on,
    )
    replayed = evaluate_validation(val_input)

    errors: list[str] = []

    # Check overall outcome
    if replayed.overall_result.value != run.overall_result.value:
        errors.append(
            f"Overall result mismatch: stored='{run.overall_result.value}', "
            f"replayed='{replayed.overall_result.value}'"
        )

    # Check ruleset version
    if replayed.ruleset_version != run.ruleset_version:
        errors.append(
            f"Ruleset version mismatch: stored='{run.ruleset_version}', "
            f"replayed='{replayed.ruleset_version}'"
        )

    # Check individual rule results
    stored_results_by_rule = {r.rule_id: r for r in run.results}
    replayed_results_by_rule = {r.rule_id: r for r in replayed.rule_results}

    all_rule_ids = sorted(set(stored_results_by_rule.keys()) | set(replayed_results_by_rule.keys()))

    for r_id in all_rule_ids:
        s_res = stored_results_by_rule.get(r_id)
        r_res = replayed_results_by_rule.get(r_id)

        if not s_res:
            errors.append(f"Rule '{r_id}' was evaluated during replay but was absent in stored run")
            continue
        if not r_res:
            errors.append(f"Rule '{r_id}' was in stored run but was not produced during replay")
            continue

        if s_res.outcome.value != r_res.outcome.value:
            errors.append(
                f"Rule '{r_id}' outcome mismatch: stored='{s_res.outcome.value}', "
                f"replayed='{r_res.outcome.value}'"
            )

        if s_res.reason_code != r_res.reason_code:
            errors.append(
                f"Rule '{r_id}' reason_code mismatch: stored='{s_res.reason_code}', "
                f"replayed='{r_res.reason_code}'"
            )

    if verbose and not errors:
        print(f"  [OK] ValidationRun {run.id} for Claim {claim.claim_ref}: {run.overall_result.value} ({len(run.results)} rules verified)")

    return len(errors) == 0, errors


def replay_risk_evaluation(
    db: Session, risk_eval: RiskEvaluation, verbose: bool = False
) -> tuple[bool, list[str]]:
    """Replay a single stored risk evaluation and verify bit-for-bit determinism."""
    claim = db.get(Claim, risk_eval.claim_id)
    if not claim:
        return False, [f"Claim '{risk_eval.claim_id}' not found for RiskEvaluation '{risk_eval.id}'"]

    service = RiskService(db)
    risk_input = service.build_risk_input(
        claim=claim,
        evaluation_date=risk_eval.evaluated_on,
    )
    replayed = evaluate_risk(risk_input)

    errors: list[str] = []

    # Check score
    if replayed.score != risk_eval.score:
        errors.append(
            f"Score mismatch: stored={risk_eval.score}, replayed={replayed.score}"
        )

    # Check category
    if replayed.category.value != risk_eval.category.value:
        errors.append(
            f"Category mismatch: stored='{risk_eval.category.value}', "
            f"replayed='{replayed.category.value}'"
        )

    # Check requires_investigation flag
    if replayed.requires_investigation != risk_eval.requires_investigation:
        errors.append(
            f"requires_investigation mismatch: stored={risk_eval.requires_investigation}, "
            f"replayed={replayed.requires_investigation}"
        )

    # Check indicator results
    stored_inds = {r.indicator_id: r for r in risk_eval.indicator_results}
    replayed_inds = {r.indicator_id: r for r in replayed.indicator_results}

    all_ind_ids = sorted(set(stored_inds.keys()) | set(replayed_inds.keys()))
    for ind_id in all_ind_ids:
        s_ind = stored_inds.get(ind_id)
        r_ind = replayed_inds.get(ind_id)

        if not s_ind:
            errors.append(f"Indicator '{ind_id}' evaluated during replay but was absent in stored evaluation")
            continue
        if not r_ind:
            errors.append(f"Indicator '{ind_id}' was in stored evaluation but was not produced during replay")
            continue

        if s_ind.triggered != r_ind.triggered:
            errors.append(
                f"Indicator '{ind_id}' triggered mismatch: stored={s_ind.triggered}, replayed={r_ind.triggered}"
            )
        if s_ind.weight_applied != r_ind.weight_applied:
            errors.append(
                f"Indicator '{ind_id}' weight_applied mismatch: stored={s_ind.weight_applied}, replayed={r_ind.weight_applied}"
            )

    if verbose and not errors:
        print(f"  [OK] RiskEvaluation {risk_eval.id} for Claim {claim.claim_ref}: score={risk_eval.score}, category={risk_eval.category.value} ({len(risk_eval.indicator_results)} indicators verified)")

    return len(errors) == 0, errors


def replay_assessment(
    db: Session, assessment: Assessment, verbose: bool = False
) -> tuple[bool, list[str]]:
    """Replay a stored assessment cap calculation and verify determinism."""
    from decimal import Decimal

    claim = db.get(Claim, assessment.claim_id)
    if not claim:
        return False, [f"Claim '{assessment.claim_id}' not found for Assessment '{assessment.id}'"]

    policy = claim.policy
    cov_stmt = select(Coverage).where(
        Coverage.policy_id == claim.policy_id,
        Coverage.claim_category == claim.claim_category,
    )
    coverage = db.scalars(cov_stmt).first()
    if not coverage:
        return False, [f"Coverage row not found for Claim '{claim.claim_ref}'"]

    trace = assessment.calculation_trace or {}
    remaining_limit = Decimal(trace.get("remaining_limit", str(coverage.limit_amount - coverage.consumed_amount)))
    deductible = Decimal(trace.get("deductible", str(coverage.deductible)))
    copay_percent = Decimal(trace.get("copay_percent", str(coverage.copay_percent)))
    depreciation = Decimal(trace.get("depreciation", "0.00"))
    sum_insured = Decimal(trace.get("sum_insured", str(policy.sum_insured)))
    value_at_risk = Decimal(trace.get("value_at_risk", "0.00"))

    inp = AssessmentInput(
        product_type=policy.product_type.value,
        requested_amount=claim.requested_amount,
        remaining_limit=remaining_limit,
        assessed_loss=assessment.assessed_loss,
        deductible=deductible,
        copay_percent=copay_percent,
        depreciation=depreciation,
        sum_insured=sum_insured,
        value_at_risk=value_at_risk,
    )
    out = calculate_assessment_cap(inp)

    errors: list[str] = []
    if out.max_eligible_amount != assessment.max_eligible_amount:
        errors.append(
            f"Max eligible amount mismatch: stored='{assessment.max_eligible_amount}', "
            f"replayed='{out.max_eligible_amount}'"
        )

    if verbose and not errors:
        print(f"  [OK] Assessment {assessment.id} for Claim {claim.claim_ref}: max_eligible={assessment.max_eligible_amount}")

    return len(errors) == 0, errors



def run_replay(
    claim_id: uuid.UUID | None = None,
    all_runs: bool = True,
    verbose: bool = False,
    db: Session | None = None,
    db_url: str | None = None,
) -> int:
    """Execute replay verification across stored validation runs and risk evaluations."""
    own_session = False
    if db is None:
        if db_url:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            custom_engine = create_engine(db_url)
            session_factory = sessionmaker(bind=custom_engine)
            db = session_factory()
        else:
            db = SessionLocal()
        own_session = True

    try:
        # 1. Validation runs
        v_stmt = select(ValidationRun).order_by(ValidationRun.created_at.asc())
        if claim_id:
            v_stmt = v_stmt.where(ValidationRun.claim_id == claim_id)
        val_runs = list(db.scalars(v_stmt).all())

        # 2. Risk evaluations
        r_stmt = select(RiskEvaluation).order_by(RiskEvaluation.created_at.asc())
        if claim_id:
            r_stmt = r_stmt.where(RiskEvaluation.claim_id == claim_id)
        risk_evals = list(db.scalars(r_stmt).all())

        # 3. Assessments
        a_stmt = select(Assessment).order_by(Assessment.created_at.asc())
        if claim_id:
            a_stmt = a_stmt.where(Assessment.claim_id == claim_id)
        assessments = list(db.scalars(a_stmt).all())

        total_runs = len(val_runs) + len(risk_evals) + len(assessments)
        if total_runs == 0:
            target = f"for claim '{claim_id}'" if claim_id else "in database"
            print(f"No validation runs, risk evaluations, or assessments found {target}. Nothing to replay.")
            return 0

        print(
            f"Starting deterministic replay for {len(val_runs)} validation run(s), "
            f"{len(risk_evals)} risk evaluation(s), and {len(assessments)} assessment(s)..."
        )

        passed_count = 0
        failed_count = 0

        for run in val_runs:
            passed, errs = replay_validation_run(db, run, verbose=verbose)
            if passed:
                passed_count += 1
            else:
                failed_count += 1
                print(f"\n[FAIL] ValidationRun {run.id} (Claim ID: {run.claim_id}):")
                for err in errs:
                    print(f"  - {err}")

        for r_eval in risk_evals:
            passed, errs = replay_risk_evaluation(db, r_eval, verbose=verbose)
            if passed:
                passed_count += 1
            else:
                failed_count += 1
                print(f"\n[FAIL] RiskEvaluation {r_eval.id} (Claim ID: {r_eval.claim_id}):")
                for err in errs:
                    print(f"  - {err}")

        for a_rec in assessments:
            passed, errs = replay_assessment(db, a_rec, verbose=verbose)
            if passed:
                passed_count += 1
            else:
                failed_count += 1
                print(f"\n[FAIL] Assessment {a_rec.id} (Claim ID: {a_rec.claim_id}):")
                for err in errs:
                    print(f"  - {err}")

        print("\nReplay Summary:")
        print(f"  Total Evaluations:  {total_runs}")
        print(f"  Passed:             {passed_count}")
        print(f"  Failed:             {failed_count}")

        return 0 if failed_count == 0 else 1
    finally:
        if own_session and db is not None:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic Engine Replay Tool")
    parser.add_argument(
        "--claim-id",
        type=uuid.UUID,
        default=None,
        help="Replay evaluations for a specific claim UUID",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=True,
        help="Replay all recorded validation runs and risk evaluations in the database",
    )
    parser.add_argument(
        "--db-url",
        type=str,
        default=None,
        help="Database connection URL (e.g. sqlite:///test.db or postgresql://...)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed per-run verification output",
    )

    args = parser.parse_args()
    exit_code = run_replay(
        claim_id=args.claim_id,
        all_runs=args.all,
        verbose=args.verbose,
        db_url=args.db_url,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

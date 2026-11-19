"""Deterministic engine replay CLI script.

Verifies pure engine determinism by replaying validation runs against database state
and asserting exact matching outcomes and reason codes.
"""

import argparse
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Claim, ValidationRun
from app.db.session import SessionLocal
from app.engine.validation import evaluate_validation
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
        print(f"  [OK] Run {run.id} for Claim {claim.claim_ref}: {run.overall_result.value} ({len(run.results)} rules verified)")

    return len(errors) == 0, errors


def run_replay(
    claim_id: uuid.UUID | None = None,
    all_runs: bool = True,
    verbose: bool = False,
    db: Session | None = None,
    db_url: str | None = None,
) -> int:
    """Execute replay verification across stored validation runs."""
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
        stmt = select(ValidationRun).order_by(ValidationRun.created_at.asc())
        if claim_id:
            stmt = stmt.where(ValidationRun.claim_id == claim_id)

        runs = list(db.scalars(stmt).all())
        if not runs:
            target = f"for claim '{claim_id}'" if claim_id else "in database"
            print(f"No validation runs found {target}. Nothing to replay.")
            return 0

        print(f"Starting deterministic replay for {len(runs)} validation run(s)...")

        passed_count = 0
        failed_count = 0

        for run in runs:
            passed, errs = replay_validation_run(db, run, verbose=verbose)
            if passed:
                passed_count += 1
            else:
                failed_count += 1
                print(f"\n[FAIL] ValidationRun {run.id} (Claim ID: {run.claim_id}):")
                for err in errs:
                    print(f"  - {err}")

        print("\nReplay Summary:")
        print(f"  Total Runs:  {len(runs)}")
        print(f"  Passed:      {passed_count}")
        print(f"  Failed:      {failed_count}")

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
        help="Replay validation runs for a specific claim UUID",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=True,
        help="Replay all recorded validation runs in the database",
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

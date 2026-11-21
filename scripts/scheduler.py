"""Platform scheduler and cron job runner (P7 Task 6).

Implements 8 operational jobs:
1. audit_verification_incremental
2. audit_verification_full
3. daily_anchor
4. reconciliation
5. cleanup_sessions
6. cleanup_login_attempts
7. backup
8. retention_report

Records every execution in the job_runs table.
Enforces the invariant that NO scheduler job may mutate claim status.
Detects and alerts on missed runs.
"""

import argparse
import logging
import sys
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit.anchor import DEFAULT_ANCHOR_DIR, create_daily_anchor_manifest
from app.audit.verifier import verify_chain
from app.db.base import utc_now
from app.db.models import (
    AuditChainHead,
    Claim,
    JobRun,
    LoginAttempt,
    UserSession,
)
from app.db.session import SessionLocal
from app.domain.enums import JobStatus
from scripts.backup_db import backup_database
from scripts.reconcile_ledger import reconcile_coverages
from scripts.retention_report import generate_retention_report

logger = logging.getLogger("scheduler")

# Job schedules and thresholds for missed-run detection
JOB_SCHEDULES: dict[str, dict[str, Any]] = {
    "audit_verification_incremental": {
        "description": "Nightly incremental audit chain verification",
        "max_interval_hours": 26,
    },
    "audit_verification_full": {
        "description": "Weekly full audit chain verification",
        "max_interval_hours": 168 + 24,  # 8 days
    },
    "daily_anchor": {
        "description": "Daily cryptographic anchor generation",
        "max_interval_hours": 26,
    },
    "reconciliation": {
        "description": "Coverage ledger vs consumed amount reconciliation",
        "max_interval_hours": 26,
    },
    "cleanup_sessions": {
        "description": "Purge expired user sessions older than 30 days",
        "max_interval_hours": 26,
    },
    "cleanup_login_attempts": {
        "description": "Purge login attempt logs older than 90 days",
        "max_interval_hours": 26,
    },
    "backup": {
        "description": "Database backup snapshot",
        "max_interval_hours": 26,
    },
    "retention_report": {
        "description": "Retention and legal hold audit report",
        "max_interval_hours": 26,
    },
}


def _run_audit_verification_incremental(db: Session) -> tuple[int, dict[str, Any]]:
    chain_keys = db.scalars(select(AuditChainHead.chain_key)).all()
    verified_count = 0
    failures: list[str] = []
    for key in chain_keys:
        res = verify_chain(db, chain_key=key)
        if res.is_valid:
            verified_count += 1
        else:
            failures.append(f"{key}: {res.error_message}")
    return verified_count, {"chains_checked": len(chain_keys), "failures": failures}


def _run_audit_verification_full(db: Session) -> tuple[int, dict[str, Any]]:
    chain_keys = db.scalars(select(AuditChainHead.chain_key)).all()
    verified_count = 0
    failures: list[str] = []
    for key in chain_keys:
        res = verify_chain(db, chain_key=key)
        if res.is_valid:
            verified_count += 1
        else:
            failures.append(f"{key}: {res.error_message}")
    return verified_count, {"chains_checked": len(chain_keys), "failures": failures}


def _run_daily_anchor(db: Session) -> tuple[int, dict[str, Any]]:
    manifest = create_daily_anchor_manifest(db=db, anchor_dir=DEFAULT_ANCHOR_DIR)
    return 1, {
        "manifest_date": manifest["manifest_date"],
        "root_hash": manifest["root_hash"],
        "chain_count": len(manifest.get("chain_heads", {})),
    }


def _run_reconciliation(db: Session) -> tuple[int, dict[str, Any]]:
    total_checked, mismatch_count, incidents = reconcile_coverages(db)
    return total_checked, {
        "total_coverages": total_checked,
        "mismatch_count": mismatch_count,
        "incident_count": len(incidents),
    }


def _run_cleanup_sessions(db: Session) -> tuple[int, dict[str, Any]]:
    cutoff = utc_now() - timedelta(days=30)
    stmt = delete(UserSession).where(UserSession.expires_at < cutoff)
    result = db.execute(stmt)
    db.commit()
    deleted = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else 0
    return deleted, {"retention_days": 30, "cutoff": cutoff.isoformat(), "deleted": deleted}


def _run_cleanup_login_attempts(db: Session) -> tuple[int, dict[str, Any]]:
    cutoff = utc_now() - timedelta(days=90)
    stmt = delete(LoginAttempt).where(LoginAttempt.attempted_at < cutoff)
    result = db.execute(stmt)
    db.commit()
    deleted = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else 0
    return deleted, {"retention_days": 90, "cutoff": cutoff.isoformat(), "deleted": deleted}


def _run_backup(db: Session) -> tuple[int, dict[str, Any]]:
    backup_path = backup_database()
    return 1, {"backup_file": str(backup_path)}


def _run_retention_report(db: Session) -> tuple[int, dict[str, Any]]:
    rep = generate_retention_report(db)
    total_evaluated = sum(rc.total_records for rc in rep.retention_classes.values())
    return total_evaluated, {
        "claims_on_legal_hold_count": rep.claims_on_legal_hold_count,
        "classes_evaluated": len(rep.retention_classes),
        "total_records_evaluated": total_evaluated,
    }


JOB_HANDLERS: dict[str, Callable[[Session], tuple[int, dict[str, Any]]]] = {
    "audit_verification_incremental": _run_audit_verification_incremental,
    "audit_verification_full": _run_audit_verification_full,
    "daily_anchor": _run_daily_anchor,
    "reconciliation": _run_reconciliation,
    "cleanup_sessions": _run_cleanup_sessions,
    "cleanup_login_attempts": _run_cleanup_login_attempts,
    "backup": _run_backup,
    "retention_report": _run_retention_report,
}


def run_job(job_name: str, db: Session) -> JobRun:
    """Execute a single scheduler job safely, recording progress in job_runs.

    Guarantees:
    - Pre and post state invariant: Claim statuses are never mutated.
    - Captures status SUCCESS or FAILED.
    """
    if job_name not in JOB_HANDLERS:
        raise ValueError(f"Unknown scheduler job: '{job_name}'")

    handler = JOB_HANDLERS[job_name]

    # Invariant snapshot: Record all claim statuses before job runs
    pre_statuses = dict(db.execute(select(Claim.id, Claim.status)).all())

    job_run = JobRun(
        id=uuid.uuid4(),
        job_name=job_name,
        status=JobStatus.RUNNING.value,
        started_at=utc_now(),
        records_processed=0,
        details={},
    )
    db.add(job_run)
    db.commit()

    try:
        records_processed, details = handler(db)

        # Invariant check: Verify claim statuses after job execution
        post_statuses = dict(db.execute(select(Claim.id, Claim.status)).all())

        if pre_statuses != post_statuses:
            raise RuntimeError(
                f"INVARIANT VIOLATION: Job '{job_name}' illegally mutated claim statuses!"
            )

        job_run.status = JobStatus.SUCCESS.value
        job_run.completed_at = utc_now()
        job_run.records_processed = records_processed
        job_run.details = details
        job_run.error_message = None
        db.commit()
    except Exception as exc:
        db.rollback()
        job_run = db.get(JobRun, job_run.id)
        if job_run:
            job_run.status = JobStatus.FAILED.value
            job_run.completed_at = utc_now()
            job_run.error_message = str(exc)
            db.commit()
        raise

    return job_run


def check_missed_runs(db: Session) -> list[dict[str, Any]]:
    """Inspect history of scheduled jobs and alert if any expected run was missed."""
    now = utc_now()
    alerts: list[dict[str, Any]] = []

    for job_name, config in JOB_SCHEDULES.items():
        max_hours = config["max_interval_hours"]
        cutoff = now - timedelta(hours=max_hours)

        last_success = db.scalars(
            select(JobRun)
            .where(JobRun.job_name == job_name, JobRun.status == JobStatus.SUCCESS.value)
            .order_by(JobRun.started_at.desc())
        ).first()

        if not last_success or (last_success.started_at and last_success.started_at < cutoff):
            last_run_time = last_success.started_at.isoformat() if last_success else "NEVER"
            alert = {
                "job_name": job_name,
                "expected_interval_hours": max_hours,
                "last_success_at": last_run_time,
                "status": "MISSED",
                "message": f"Job '{job_name}' has not succeeded within {max_hours}h (last run: {last_run_time})",
            }
            alerts.append(alert)

            # Record missed run entry
            missed_run = JobRun(
                id=uuid.uuid4(),
                job_name=job_name,
                status=JobStatus.MISSED.value,
                started_at=now,
                completed_at=now,
                records_processed=0,
                details={"alert": alert},
                error_message="Missed scheduled run threshold",
            )
            db.add(missed_run)
            db.commit()

    return alerts


def main() -> None:
    parser = argparse.ArgumentParser(description="Platform scheduler job runner")
    parser.add_argument(
        "--db-url",
        help="Database URL (defaults to configured settings.DATABASE_URL)",
    )
    parser.add_argument(
        "--job",
        choices=list(JOB_HANDLERS.keys()),
        help="Run a specific scheduled job",
    )
    parser.add_argument(
        "--run-all",
        action="store_true",
        help="Execute all 8 operational jobs sequentially",
    )
    parser.add_argument(
        "--check-missed",
        action="store_true",
        help="Check for missed job runs and emit alerts",
    )
    args = parser.parse_args()

    if args.db_url:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(args.db_url)
        session_maker = sessionmaker(bind=engine)
        db = session_maker()
    else:
        db = SessionLocal()
    try:
        if args.check_missed:
            alerts = check_missed_runs(db)
            if alerts:
                print(f"⚠️  {len(alerts)} missed jobs detected:")
                for alert in alerts:
                    print(f"   - {alert['job_name']}: {alert['message']}")
            else:
                print("✅ All scheduled jobs are within acceptable runtime thresholds.")
            return

        if args.run_all:
            print(f"Executing all {len(JOB_HANDLERS)} scheduled jobs...")
            for name in JOB_HANDLERS:
                print(f"Running '{name}'...")
                res = run_job(name, db)
                print(f"   Status: {res.status}, records: {res.records_processed}")
            return

        if args.job:
            print(f"Running scheduled job '{args.job}'...")
            res = run_job(args.job, db)
            print(f"✅ Job finished with status {res.status}, records: {res.records_processed}")
            return

        parser.print_help()
    finally:
        db.close()


if __name__ == "__main__":
    main()

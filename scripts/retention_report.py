"""Retention report generation script (Phase P7 Task 5, Decision D10).

Report-only tooling that calculates data retention status across domain entities based
on configured Decision D10 periods. Never performs any data deletions.
Respects the legal_hold flag on claims and all related records.
"""

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    Claim,
    ClaimStatusHistory,
    ImportReject,
    JobRun,
    LoginAttempt,
    UserSession,
    VerificationRun,
)
from app.db.session import SessionLocal
from app.domain.state_machine import FINAL_STATES

# Default retention periods per Decision D10
RETENTION_PERIODS = {
    "CLAIM_FINAL_YEARS": 10,
    "IMPORT_REJECTS_DAYS": 365,
    "VERIFICATION_RUNS_DAYS": 730,
    "JOB_RUNS_DAYS": 730,
    "LOGIN_ATTEMPTS_DAYS": 90,
    "SESSIONS_EXPIRED_DAYS": 30,
    "BACKUPS_DAYS": 30,
}


@dataclass
class RetentionClassSummary:
    class_name: str
    retention_period: str
    total_records: int = 0
    retained_active_count: int = 0
    eligible_for_review_count: int = 0
    held_by_legal_hold_count: int = 0
    oldest_record_date: str | None = None
    newest_record_date: str | None = None


@dataclass
class RetentionReport:
    report_generated_at: str
    as_of_date: str
    retention_classes: dict[str, RetentionClassSummary] = field(default_factory=dict)
    claims_on_legal_hold_count: int = 0
    summary_notes: list[str] = field(default_factory=list)


def generate_retention_report(db: Session, as_of: date | None = None) -> RetentionReport:
    """Generate comprehensive, non-destructive retention analysis."""
    ref_date = as_of or datetime.now(UTC).date()
    now_utc = datetime.now(UTC)

    report = RetentionReport(
        report_generated_at=now_utc.isoformat(),
        as_of_date=ref_date.isoformat(),
        summary_notes=[
            "DISCLAIMER: This is a report-only tool per Decision D10.",
            "No data is modified or deleted by this tool.",
            "Erasure in v1 requires a documented two-party approval procedure.",
        ],
    )

    # 1. Claims and dependent records (10 years after final status)
    cutoff_10y = ref_date - timedelta(days=365 * RETENTION_PERIODS["CLAIM_FINAL_YEARS"])
    claims = list(db.scalars(select(Claim)).all())

    claim_summary = RetentionClassSummary(
        class_name="Claims (and related assessments, decisions, investigations, ledger, docs)",
        retention_period=f"{RETENTION_PERIODS['CLAIM_FINAL_YEARS']} years post-final status",
        total_records=len(claims),
    )

    legal_hold_claim_ids: set[Any] = set()
    eligible_claim_ids: set[Any] = set()

    for clm in claims:
        if clm.legal_hold:
            legal_hold_claim_ids.add(clm.id)
            claim_summary.held_by_legal_hold_count += 1
            continue

        if clm.status.value in FINAL_STATES:
            # Determine when claim reached final status from history
            final_hist = db.scalars(
                select(ClaimStatusHistory)
                .where(
                    ClaimStatusHistory.claim_id == clm.id,
                    ClaimStatusHistory.to_status.in_(FINAL_STATES),
                )
                .order_by(ClaimStatusHistory.created_at.desc())
            ).first()

            final_date = (
                final_hist.created_at.date()
                if final_hist
                else (clm.updated_at.date() if clm.updated_at else clm.registered_on)
            )

            if final_date <= cutoff_10y:
                eligible_claim_ids.add(clm.id)
                claim_summary.eligible_for_review_count += 1
            else:
                claim_summary.retained_active_count += 1
        else:
            claim_summary.retained_active_count += 1

    report.claims_on_legal_hold_count = len(legal_hold_claim_ids)
    report.retention_classes["claims"] = claim_summary

    # 2. Audit logs (same as claim they belong to, or 10 years)
    audit_logs = list(db.scalars(select(AuditLog)).all())
    audit_summary = RetentionClassSummary(
        class_name="Audit Logs",
        retention_period="Aligned with referenced claim lifecycle (or 10 years general)",
        total_records=len(audit_logs),
    )
    for log in audit_logs:
        if log.claim_id in legal_hold_claim_ids:
            audit_summary.held_by_legal_hold_count += 1
        elif log.claim_id in eligible_claim_ids:
            audit_summary.eligible_for_review_count += 1
        else:
            log_date = log.occurred_at.date() if log.occurred_at else None
            if log_date and log_date <= cutoff_10y:
                audit_summary.eligible_for_review_count += 1
            else:
                audit_summary.retained_active_count += 1
    report.retention_classes["audit_logs"] = audit_summary

    # 3. Import Rejects (1 year)
    cutoff_rejects = ref_date - timedelta(days=RETENTION_PERIODS["IMPORT_REJECTS_DAYS"])
    rejects = list(db.scalars(select(ImportReject)).all())
    rej_summary = RetentionClassSummary(
        class_name="Import Rejects",
        retention_period=f"{RETENTION_PERIODS['IMPORT_REJECTS_DAYS']} days",
        total_records=len(rejects),
    )
    for rej in rejects:
        rej_date = rej.created_at.date() if rej.created_at else None
        if rej_date and rej_date <= cutoff_rejects:
            rej_summary.eligible_for_review_count += 1
        else:
            rej_summary.retained_active_count += 1
    report.retention_classes["import_rejects"] = rej_summary

    # 4. Job Runs (2 years)
    cutoff_jobs = ref_date - timedelta(days=RETENTION_PERIODS["JOB_RUNS_DAYS"])
    jobs = list(db.scalars(select(JobRun)).all())
    jobs_summary = RetentionClassSummary(
        class_name="Job Runs",
        retention_period=f"{RETENTION_PERIODS['JOB_RUNS_DAYS']} days",
        total_records=len(jobs),
    )
    for j in jobs:
        j_date = j.started_at.date() if j.started_at else None
        if j_date and j_date <= cutoff_jobs:
            jobs_summary.eligible_for_review_count += 1
        else:
            jobs_summary.retained_active_count += 1
    report.retention_classes["job_runs"] = jobs_summary

    # 5. Verification Runs (2 years)
    vruns = list(db.scalars(select(VerificationRun)).all())
    vruns_summary = RetentionClassSummary(
        class_name="Verification Runs",
        retention_period=f"{RETENTION_PERIODS['VERIFICATION_RUNS_DAYS']} days",
        total_records=len(vruns),
    )
    for vr in vruns:
        vr_date = vr.started_at.date() if vr.started_at else None
        if vr_date and vr_date <= cutoff_jobs:
            vruns_summary.eligible_for_review_count += 1
        else:
            vruns_summary.retained_active_count += 1
    report.retention_classes["verification_runs"] = vruns_summary

    # 6. Login Attempts (90 days)
    cutoff_logins = ref_date - timedelta(days=RETENTION_PERIODS["LOGIN_ATTEMPTS_DAYS"])
    logins = list(db.scalars(select(LoginAttempt)).all())
    login_summary = RetentionClassSummary(
        class_name="Login Attempts",
        retention_period=f"{RETENTION_PERIODS['LOGIN_ATTEMPTS_DAYS']} days",
        total_records=len(logins),
    )
    for attempt in logins:
        l_date = attempt.attempted_at.date() if attempt.attempted_at else None
        if l_date and l_date <= cutoff_logins:
            login_summary.eligible_for_review_count += 1
        else:
            login_summary.retained_active_count += 1
    report.retention_classes["login_attempts"] = login_summary

    # 7. User Sessions (30 days post-expiry)
    cutoff_sessions = ref_date - timedelta(days=RETENTION_PERIODS["SESSIONS_EXPIRED_DAYS"])
    sessions = list(db.scalars(select(UserSession)).all())
    session_summary = RetentionClassSummary(
        class_name="User Sessions",
        retention_period=f"{RETENTION_PERIODS['SESSIONS_EXPIRED_DAYS']} days post-expiry",
        total_records=len(sessions),
    )
    for s in sessions:
        exp_date = s.expires_at.date() if s.expires_at else None
        if exp_date and exp_date <= cutoff_sessions:
            session_summary.eligible_for_review_count += 1
        else:
            session_summary.retained_active_count += 1
    report.retention_classes["user_sessions"] = session_summary

    return report


def render_report_text(report: RetentionReport) -> str:
    """Format report into human-readable text."""
    lines = [
        "=" * 80,
        "PLATFORM DATA RETENTION AUDIT REPORT (REPORT-ONLY)",
        "=" * 80,
        f"Generated At: {report.report_generated_at}",
        f"As-Of Date:   {report.as_of_date}",
        f"Claims Under Active Legal Hold: {report.claims_on_legal_hold_count}",
        "",
        "SUMMARY OF RETENTION STATUS BY CLASS:",
        "-" * 80,
        f"{'Class Name':<35} | {'Period':<15} | {'Total':<7} | {'Active':<7} | {'Eligible':<8} | {'Legal Hold':<10}",
        "-" * 80,
    ]

    for _key, c in report.retention_classes.items():
        lines.append(
            f"{c.class_name[:35]:<35} | {c.retention_period[:15]:<15} | "
            f"{c.total_records:<7} | {c.retained_active_count:<7} | "
            f"{c.eligible_for_review_count:<8} | {c.held_by_legal_hold_count:<10}"
        )

    lines.extend(
        [
            "-" * 80,
            "",
            "NOTES & POLICY DIRECTIVES:",
        ]
    )
    for note in report.summary_notes:
        lines.append(f"- {note}")
    lines.append("=" * 80)
    return "\n".join(lines)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate data retention report.")
    parser.add_argument("--json", action="store_true", help="Output in JSON format.")
    parser.add_argument("--as-of", type=str, help="As-of reference date (YYYY-MM-DD).")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else None

    with SessionLocal() as db:
        report = generate_retention_report(db, as_of=as_of)

    if args.json:
        # Convert dataclass to dict
        report_dict = asdict(report)
        print(json.dumps(report_dict, indent=2))
    else:
        print(render_report_text(report))


if __name__ == "__main__":
    main()

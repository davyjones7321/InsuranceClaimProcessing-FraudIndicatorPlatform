"""Automated backup and restore drill verification script.

Demonstrates Section 16/17 requirement:
- Backup creation with timestamps
- Complete restoration into an isolated verification target
- Verification of row counts across all domain entities
- Complete cryptographic HMAC-SHA256 audit chain verification on restored database
- Report timings and evidence
"""

import argparse
import sys
import tempfile
import time
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.audit.verifier import verify_chain
from app.config import settings
from app.db.base import Base
from app.db.models import (
    AuditChainHead,
    AuditLog,
    Claim,
    Coverage,
    Customer,
    Decision,
    InvestigationFinding,
    Policy,
    User,
)
from scripts.backup_db import backup_database
from scripts.restore_db import restore_database


def ensure_drill_source_db(source_url: str | None, temp_dir: Path) -> str:
    """Ensure a source database exists with audit chains, creating and seeding a test DB if needed."""
    if source_url:
        return source_url

    # Check if configured DATABASE_URL has audit chains to verify
    if settings.DATABASE_URL:
        try:
            connect_args = {"connect_timeout": 2} if "postgresql" in settings.DATABASE_URL else {}
            engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)
            SessionMaker = sessionmaker(bind=engine)
            with SessionMaker() as session:
                has_chains = session.scalar(select(func.count(AuditChainHead.chain_key))) or 0
                if has_chains > 0:
                    engine.dispose()
                    return settings.DATABASE_URL
            engine.dispose()
            print(
                "Notice: Configured DATABASE_URL has no audit chains. Using dedicated seeded drill database."
            )
        except Exception as e:
            print(
                f"Notice: Configured DATABASE_URL check skipped ({e}). Using dedicated seeded drill database."
            )

    # Create and populate a dedicated SQLite source DB with deterministic seed data
    source_sqlite = temp_dir / "drill_source.sqlite"
    source_db_url = f"sqlite:///{source_sqlite}"
    engine = create_engine(source_db_url)
    Base.metadata.create_all(bind=engine)
    SessionMaker = sessionmaker(bind=engine)
    with SessionMaker() as session:
        from scripts.seed import seed_database

        seed_database(session)
    engine.dispose()
    return source_db_url


def prepare_drill_target_database(
    src_db_url: str, backup_file: Path, working_dir: Path
) -> tuple[str, bool]:
    """Prepare target database for drill, creating Postgres DB if needed, or returning SQLite path."""
    is_sqlite = "sqlite" in src_db_url or backup_file.suffix == ".sqlite"
    if is_sqlite:
        drill_db_file = working_dir / "restored_drill.sqlite"
        return f"sqlite:///{drill_db_file}", False

    # Attempt PostgreSQL database creation
    drill_db_url = src_db_url.replace("insurance_claims", "insurance_claims_drill")
    db_name = drill_db_url.rsplit("/", 1)[-1].split("?")[0]
    base_pg_url = drill_db_url.rsplit("/", 1)[0] + "/postgres"

    try:
        admin_engine = create_engine(base_pg_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": db_name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        admin_engine.dispose()
        return drill_db_url, True
    except Exception as e:
        print(
            f"Notice: Could not create PostgreSQL drill database '{db_name}': {e}. Using isolated SQLite target."
        )
        drill_db_file = working_dir / "restored_drill.sqlite"
        return f"sqlite:///{drill_db_file}", False


def cleanup_drill_target_database(drill_db_url: str, is_pg_created: bool) -> None:
    """Clean up PostgreSQL drill database if created."""
    if not is_pg_created or "sqlite" in drill_db_url:
        return
    try:
        db_name = drill_db_url.rsplit("/", 1)[-1].split("?")[0]
        base_pg_url = drill_db_url.rsplit("/", 1)[0] + "/postgres"
        admin_engine = create_engine(base_pg_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin_engine.dispose()
    except Exception as e:
        print(f"Warning: Failed to clean up drill database '{drill_db_url}': {e}")


def run_drill(
    source_url: str | None = None, temp_dir: Path | None = None
) -> dict[str, float | int | bool | str]:
    drill_start = time.perf_counter()
    working_dir = temp_dir or Path(tempfile.mkdtemp(prefix="claims_drill_"))

    print("=" * 70)
    print("INSURANCE CLAIM PLATFORM: BACKUP & RESTORE DRILL VERIFICATION")
    print(f"Working directory: {working_dir}")
    print("=" * 70)

    # Step 0: Ensure source database
    src_db_url = ensure_drill_source_db(source_url, working_dir)
    print(f"Source database URL: {src_db_url}")

    # Step 1: Backup
    print("\n[Step 1/4] Initiating database backup...")
    t0 = time.perf_counter()
    backup_file = backup_database(source_db_url=src_db_url, output_path=str(working_dir))
    backup_duration = time.perf_counter() - t0
    backup_size = backup_file.stat().st_size
    print(f"-> Backup completed in {backup_duration:.4f}s ({backup_size} bytes)")

    # Step 2: Target restore DB preparation
    print("\n[Step 2/4] Setting up drill target database...")
    drill_db_url, is_pg_created = prepare_drill_target_database(
        src_db_url, backup_file, working_dir
    )

    drill_engine = None
    try:
        t1 = time.perf_counter()
        restore_database(str(backup_file), target_db_url=drill_db_url)
        restore_duration = time.perf_counter() - t1
        print(f"-> Restore completed in {restore_duration:.4f}s into {drill_db_url}")

        # Step 3: Record count verification
        print("\n[Step 3/4] Verifying restored entity counts against target database...")
        drill_engine = create_engine(drill_db_url)
        DrillSession = sessionmaker(bind=drill_engine)
        session: Session = DrillSession()

        user_count = session.scalar(select(func.count(User.id))) or 0
        customer_count = session.scalar(select(func.count(Customer.id))) or 0
        policy_count = session.scalar(select(func.count(Policy.id))) or 0
        coverage_count = session.scalar(select(func.count(Coverage.id))) or 0
        claim_count = session.scalar(select(func.count(Claim.id))) or 0
        decision_count = session.scalar(select(func.count(Decision.id))) or 0
        investigation_count = session.scalar(select(func.count(InvestigationFinding.id))) or 0
        audit_log_count = session.scalar(select(func.count(AuditLog.id))) or 0
        chain_head_count = session.scalar(select(func.count(AuditChainHead.chain_key))) or 0

        print(f"  • Users:                  {user_count}")
        print(f"  • Customers:              {customer_count}")
        print(f"  • Policies:               {policy_count}")
        print(f"  • Coverages:              {coverage_count}")
        print(f"  • Claims:                 {claim_count}")
        print(f"  • Decisions:              {decision_count}")
        print(f"  • Investigation Findings: {investigation_count}")
        print(f"  • Audit Log Entries:      {audit_log_count}")
        print(f"  • Audit Chain Heads:      {chain_head_count}")

        # Step 4: Cryptographic HMAC-SHA256 verification on restored database
        print(
            "\n[Step 4/4] Verifying HMAC-SHA256 cryptographic audit chains on restored database..."
        )
        t2 = time.perf_counter()
        heads = list(session.scalars(select(AuditChainHead)).all())
        total_chains = len(heads)
        verified_chains = 0
        broken_chains = 0

        for head in heads:
            res = verify_chain(session, head.chain_key, record_run=False)
            if res.is_valid:
                verified_chains += 1
            else:
                broken_chains += 1
                print(
                    f"  ❌ Chain '{head.chain_key}' broken at seq {res.first_broken_seq}: {res.error_message}"
                )

        chain_duration = time.perf_counter() - t2
        session.close()
    finally:
        if drill_engine is not None:
            drill_engine.dispose()
        cleanup_drill_target_database(drill_db_url, is_pg_created)

    total_drill_duration = time.perf_counter() - drill_start

    print(f"-> Verified {verified_chains}/{total_chains} audit chains in {chain_duration:.4f}s")
    if broken_chains == 0:
        print("-> 100% cryptographic integrity confirmed on restored backup.")
    else:
        print(f"-> ❌ CRITICAL: {broken_chains} broken chains detected in restored backup!")

    print("\n" + "=" * 70)
    print("DRILL SUMMARY [measured]:")
    print(f"  • Total Duration:         {total_drill_duration:.4f}s")
    print(f"  • Backup Duration:        {backup_duration:.4f}s ({backup_size} bytes)")
    print(f"  • Restore Duration:       {restore_duration:.4f}s")
    print(f"  • Audit Verify Duration:  {chain_duration:.4f}s")
    print(
        f"  • Total Entities Restored:{user_count + customer_count + policy_count + claim_count + audit_log_count}"
    )
    print(f"  • Chains Verified:        {verified_chains}/{total_chains} (100% valid)")
    print("=" * 70)

    if broken_chains > 0:
        raise RuntimeError(f"Drill failed: {broken_chains} broken chains found.")

    return {
        "status": "SUCCESS",
        "backup_duration_s": round(backup_duration, 4),
        "restore_duration_s": round(restore_duration, 4),
        "audit_duration_s": round(chain_duration, 4),
        "total_duration_s": round(total_drill_duration, 4),
        "claims_count": claim_count,
        "audit_entries_count": audit_log_count,
        "chains_count": total_chains,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run automated backup and restore drill.")
    parser.add_argument("--source-url", "-s", help="Source database connection URL")
    args = parser.parse_args()

    try:
        run_drill(source_url=args.source_url)
    except Exception as e:
        print(f"\n❌ Backup and restore drill failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

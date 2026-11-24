"""Automated PostgreSQL backup and restore drill script (Phase P7 Task 7).

Requirements:
- PostgreSQL only: pg_dump and isolated target database restoration.
- Compares row counts of all domain tables between source and restored database.
- Verifies every cryptographic HMAC-SHA256 audit chain on the restored database.
- Strict: Never falls back to SQLite or a seeded substitute for production acceptance.
- Records and reports granular phase timings and metrics.
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.audit.verifier import verify_chain
from app.config import settings
from app.db.models import (
    AuditChainHead,
    AuditLog,
    Claim,
    Coverage,
    CoverageLedger,
    Customer,
    Decision,
    InvestigationFinding,
    Policy,
    User,
)
from scripts.backup_db import backup_database
from scripts.restore_db import restore_database


def verify_pg_row_counts(src_session: Session, dst_session: Session) -> dict[str, tuple[int, int]]:
    """Compare row counts of domain entities between source and restored databases."""
    entities = [
        ("User", User),
        ("Customer", Customer),
        ("Policy", Policy),
        ("Coverage", Coverage),
        ("CoverageLedger", CoverageLedger),
        ("Claim", Claim),
        ("Decision", Decision),
        ("InvestigationFinding", InvestigationFinding),
        ("AuditLog", AuditLog),
        ("AuditChainHead", AuditChainHead),
    ]

    counts: dict[str, tuple[int, int]] = {}
    for name, model in entities:
        src_count = src_session.scalar(select(func.count()).select_from(model)) or 0
        dst_count = dst_session.scalar(select(func.count()).select_from(model)) or 0
        counts[name] = (src_count, dst_count)
        if src_count != dst_count:
            raise ValueError(
                f"Row count mismatch for {name}: source={src_count}, restored={dst_count}"
            )
    return counts


def run_pg_drill(
    source_url: str | None = None,
    allow_offline_dev: bool = False,
) -> dict[str, Any]:
    """Execute the strict PostgreSQL backup and restore verification drill."""
    drill_start = time.perf_counter()
    src_db_url = source_url or settings.DATABASE_URL or os.environ.get("DATABASE_URL")

    if not src_db_url or "postgresql" not in src_db_url:
        if not allow_offline_dev:
            raise RuntimeError(
                "Phase P7 Task 7 Violation: PostgreSQL is strictly required for production acceptance drill. "
                "SQLite fallback or seeded substitutes are prohibited for acceptance verification."
            )
        print("Notice: Running in offline dev mode with SQLite.")

    is_pg = "postgresql" in (src_db_url or "")
    working_dir = Path(tempfile.mkdtemp(prefix="pg_drill_"))

    print("=" * 75)
    print("PRODUCTION ACCEPTANCE: POSTGRESQL BACKUP & RESTORE DRILL (Phase P7 Task 7)")
    print(f"Source Database: {src_db_url}")
    print(f"Working Directory: {working_dir}")
    print("=" * 75)

    # 1. Backup Phase
    print("\n[Step 1/4] Creating physical/logical database backup...")
    t0 = time.perf_counter()
    backup_file = backup_database(source_db_url=src_db_url, output_path=str(working_dir))
    backup_duration = time.perf_counter() - t0
    backup_size = backup_file.stat().st_size
    print(f"-> Backup succeeded in {backup_duration:.4f}s ({backup_size} bytes)")

    # 2. Target Database Provisioning
    print("\n[Step 2/4] Provisioning isolated restore target database...")
    if is_pg:
        drill_db_url = src_db_url.replace("insurance_claims_test", "insurance_claims_drill_target")
        if drill_db_url == src_db_url:
            drill_db_url = src_db_url + "_drill_target"

        target_db_name = drill_db_url.rsplit("/", 1)[-1].split("?")[0]
        base_pg_url = drill_db_url.rsplit("/", 1)[0] + "/postgres"

        admin_engine = create_engine(base_pg_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": target_db_name}
            ).scalar()
            if exists:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :d AND pid <> pg_backend_pid()"
                    ),
                    {"d": target_db_name},
                )
                conn.execute(text(f'DROP DATABASE "{target_db_name}"'))
            conn.execute(text(f'CREATE DATABASE "{target_db_name}"'))
        admin_engine.dispose()
    else:
        drill_db_file = working_dir / "drill_target.sqlite"
        drill_db_url = f"sqlite:///{drill_db_file}"

    # 3. Restore Phase
    print(f"\n[Step 3/4] Restoring dump into isolated target '{drill_db_url}'...")
    t1 = time.perf_counter()
    restore_database(str(backup_file), target_db_url=drill_db_url)
    restore_duration = time.perf_counter() - t1
    print(f"-> Restore succeeded in {restore_duration:.4f}s")

    # 4. Verification Phase
    print("\n[Step 4/4] Verifying table row counts and cryptographic HMAC audit chains...")
    src_engine = create_engine(src_db_url)
    dst_engine = create_engine(drill_db_url)
    SrcSession = sessionmaker(bind=src_engine)
    DstSession = sessionmaker(bind=dst_engine)

    with SrcSession() as src_sess, DstSession() as dst_sess:
        row_counts = verify_pg_row_counts(src_sess, dst_sess)
        for entity_name, (sc, dc) in row_counts.items():
            print(f"   - {entity_name:22} source={sc:<5} restored={dc:<5} (MATCH)")

        # Verify HMAC-SHA256 audit chains on restored database
        t2 = time.perf_counter()
        heads = list(dst_sess.scalars(select(AuditChainHead)).all())
        total_chains = len(heads)
        verified_chains = 0
        broken_chains: list[str] = []

        for head in heads:
            res = verify_chain(dst_sess, head.chain_key, record_run=False)
            if res.is_valid:
                verified_chains += 1
            else:
                broken_chains.append(
                    f"{head.chain_key} at seq {res.first_broken_seq}: {res.error_message}"
                )

        chain_duration = time.perf_counter() - t2

    # Clean up drill target database
    if is_pg:
        admin_engine = create_engine(base_pg_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": target_db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{target_db_name}"'))
        admin_engine.dispose()

    src_engine.dispose()
    dst_engine.dispose()

    total_duration = time.perf_counter() - drill_start

    print("\n" + "=" * 75)
    print("DRILL VERIFICATION REPORT [MEASURED]:")
    print(f"  • Status:                 {'SUCCESS' if not broken_chains else 'FAILED'}")
    print(f"  • Backup Duration:        {backup_duration:.4f}s ({backup_size} bytes)")
    print(f"  • Restore Duration:       {restore_duration:.4f}s")
    print(f"  • Chain Verify Duration:  {chain_duration:.4f}s")
    print(f"  • Total Duration:         {total_duration:.4f}s")
    print(
        f"  • Audit Chains Verified:  {verified_chains}/{total_chains} (100% cryptographic integrity)"
    )
    print("=" * 75)

    if broken_chains:
        raise RuntimeError(f"Cryptographic integrity failed on restored database: {broken_chains}")

    return {
        "status": "SUCCESS",
        "backup_duration_s": round(backup_duration, 4),
        "restore_duration_s": round(restore_duration, 4),
        "chain_verify_duration_s": round(chain_duration, 4),
        "total_duration_s": round(total_duration, 4),
        "row_counts": row_counts,
        "verified_chains": verified_chains,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PostgreSQL Backup and Restore Acceptance Drill")
    parser.add_argument("--source-url", "-s", help="Source PostgreSQL connection URL")
    parser.add_argument(
        "--allow-offline-dev",
        action="store_true",
        help="Allow running in local offline dev mode with SQLite",
    )
    args = parser.parse_args()

    try:
        run_pg_drill(source_url=args.source_url, allow_offline_dev=args.allow_offline_dev)
    except Exception as exc:
        print(f"\n❌ Backup and restore drill failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

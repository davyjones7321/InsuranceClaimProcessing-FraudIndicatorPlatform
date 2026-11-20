"""Coverage ledger reconciliation script.

Verifies consumed_amount on each Coverage equals sum(CONSUME) - sum(REVERSE) from coverage_ledger.
Creates an incident record on mismatch and never auto-fixes. Safe to rerun.
"""

import argparse
import json
import logging
import sys
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Coverage, CoverageLedger
from app.db.session import SessionLocal
from app.domain.enums import CoverageLedgerEntryType

logger = logging.getLogger("reconcile_ledger")


def reconcile_coverages(
    db: Session,
    coverage_id: uuid.UUID | None = None,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Reconcile coverage consumed_amount against coverage_ledger entries.

    Returns:
        (total_checked, mismatch_count, incidents)
    """
    stmt = select(Coverage).order_by(Coverage.id)
    if coverage_id:
        stmt = stmt.where(Coverage.id == coverage_id)

    coverages = list(db.scalars(stmt).all())
    total_checked = len(coverages)
    mismatch_count = 0
    incidents: list[dict[str, Any]] = []

    for cov in coverages:
        consume_sum = db.scalar(
            select(func.coalesce(func.sum(CoverageLedger.amount), Decimal("0.00"))).where(
                CoverageLedger.coverage_id == cov.id,
                CoverageLedger.entry_type == CoverageLedgerEntryType.CONSUME,
            )
        ) or Decimal("0.00")

        reverse_sum = db.scalar(
            select(func.coalesce(func.sum(CoverageLedger.amount), Decimal("0.00"))).where(
                CoverageLedger.coverage_id == cov.id,
                CoverageLedger.entry_type == CoverageLedgerEntryType.REVERSE,
            )
        ) or Decimal("0.00")

        expected_consumed = consume_sum - reverse_sum
        actual_consumed = cov.consumed_amount

        if actual_consumed != expected_consumed:
            mismatch_count += 1
            incident = {
                "incident_type": "COVERAGE_LEDGER_MISMATCH",
                "coverage_id": str(cov.id),
                "policy_id": str(cov.policy_id),
                "claim_category": cov.claim_category,
                "actual_consumed": str(actual_consumed),
                "expected_consumed": str(expected_consumed),
                "consume_sum": str(consume_sum),
                "reverse_sum": str(reverse_sum),
                "delta": str(actual_consumed - expected_consumed),
            }
            incidents.append(incident)
            logger.error(
                "RECONCILIATION MISMATCH: Coverage %s (%s) actual=%s != ledger=%s (delta=%s)",
                cov.id,
                cov.claim_category,
                actual_consumed,
                expected_consumed,
                actual_consumed - expected_consumed,
            )

    return total_checked, mismatch_count, incidents


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile coverage ledger against coverage consumed_amount"
    )
    parser.add_argument(
        "--coverage-id", type=uuid.UUID, default=None, help="Specific coverage UUID to reconcile"
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true", help="Output results in JSON format"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db: Session = SessionLocal()
    try:
        total, mismatches, incidents = reconcile_coverages(db, coverage_id=args.coverage_id)

        report = {
            "status": "PASS" if mismatches == 0 else "FAIL",
            "total_coverages_checked": total,
            "mismatch_count": mismatches,
            "incidents": incidents,
        }

        if args.json_output:
            print(json.dumps(report, indent=2))
        else:
            print(f"Coverage Reconciliation Complete: {total} checked, {mismatches} mismatches.")
            if mismatches > 0:
                print("INCIDENTS DETECTED (no auto-fix applied):")
                for inc in incidents:
                    print(
                        f"  Coverage {inc['coverage_id']} ({inc['claim_category']}): "
                        f"actual={inc['actual_consumed']}, expected={inc['expected_consumed']}, delta={inc['delta']}"
                    )

        return 0 if mismatches == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

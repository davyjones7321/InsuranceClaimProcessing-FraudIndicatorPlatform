"""CLI and utility script for reporting stale claims (Phase P4 Task 6).

Generates a human-facing report of claims remaining in non-final states
past a defined aging threshold (default 30 days).
Strictly read-only: must not mutate claim state or trigger automatic status transitions.
"""

import argparse
import json
import sys
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.models import Claim
from app.db.session import SessionLocal
from app.domain.enums import ClaimStatus

TERMINAL_STATUSES: frozenset[ClaimStatus] = frozenset(
    {
        ClaimStatus.APPROVED,
        ClaimStatus.REJECTED,
        ClaimStatus.WITHDRAWN,
    }
)


def get_stale_claims(db: Session, threshold_days: int = 30) -> list[dict[str, Any]]:
    """Query claims in non-final states that have been open longer than threshold_days.

    Strictly read-only; performs no mutations.
    """
    now = utc_now()
    cutoff = now - timedelta(days=threshold_days)

    stmt = (
        select(Claim)
        .where(
            Claim.status.not_in(TERMINAL_STATUSES),
            Claim.registered_at <= cutoff,
        )
        .order_by(Claim.registered_at.asc())
    )
    claims = list(db.scalars(stmt).all())

    results: list[dict[str, Any]] = []
    for c in claims:
        days_open = (now - c.registered_at).days
        results.append(
            {
                "claim_id": str(c.id),
                "claim_ref": c.claim_ref,
                "status": c.status.value,
                "customer_id": str(c.customer_id),
                "policy_id": str(c.policy_id),
                "claim_category": c.claim_category,
                "requested_amount": str(c.requested_amount),
                "registered_at": c.registered_at.isoformat(),
                "days_open": days_open,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate report of stale claims in non-final states."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Aging threshold in days (default: 30)",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        stale_list = get_stale_claims(db=db, threshold_days=args.days)
        if args.format == "json":
            print(json.dumps(stale_list, indent=2))
        else:
            print(f"=== STALE CLAIMS REPORT (Threshold: >= {args.days} days) ===")
            print(f"Total Stale Claims: {len(stale_list)}\n")
            if not stale_list:
                print("No stale claims found.")
                return

            header = f"{'Claim Ref':<18} | {'Status':<22} | {'Days Open':<10} | {'Amount':<14} | {'Registered At':<26}"
            print(header)
            print("-" * len(header))
            for item in stale_list:
                print(
                    f"{item['claim_ref']:<18} | {item['status']:<22} | {item['days_open']:<10} | "
                    f"{item['requested_amount']:<14} | {item['registered_at']:<26}"
                )
    except Exception as exc:
        print(f"Error generating stale claims report: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

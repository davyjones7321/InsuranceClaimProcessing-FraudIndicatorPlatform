"""Verification script that checks HMAC-SHA256 audit chain integrity."""

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.verifier import verify_chain
from app.db.models import AuditChainHead
from app.db.session import SessionLocal
from app.domain.enums import VerificationRunType


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify audit chain cryptographic integrity.")
    parser.add_argument(
        "--chain",
        default="all",
        help="Chain key to verify (e.g. 'all', 'system', or 'claim:<uuid>')",
    )
    parser.add_argument("--anchor", help="Expected external anchor hash")
    args = parser.parse_args()

    db: Session = SessionLocal()
    has_error = False

    try:
        if args.chain == "all":
            heads = list(db.scalars(select(AuditChainHead)).all())
            if not heads:
                print("No audit chains found.")
                return

            print(f"Verifying {len(heads)} audit chains...")
            for head in heads:
                res = verify_chain(
                    db, head.chain_key, record_run=True, run_type=VerificationRunType.FULL
                )
                if not res.is_valid:
                    print(
                        f"❌ FAIL: Chain '{res.chain_key}' broken at seq {res.first_broken_seq}: {res.error_message}"
                    )
                    has_error = True
                else:
                    print(f"✅ PASS: Chain '{res.chain_key}' verified ({res.total_events} events)")
        else:
            res = verify_chain(
                db,
                args.chain,
                record_run=True,
                run_type=VerificationRunType.FULL,
                external_anchor_hash=args.anchor,
            )
            if not res.is_valid:
                print(
                    f"❌ FAIL: Chain '{res.chain_key}' broken at seq {res.first_broken_seq}: {res.error_message}"
                )
                has_error = True
            else:
                print(f"✅ PASS: Chain '{res.chain_key}' verified ({res.total_events} events)")

        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Verification aborted with error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()

    if has_error:
        sys.exit(2)


if __name__ == "__main__":
    main()

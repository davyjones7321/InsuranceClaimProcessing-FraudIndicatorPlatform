"""Verification script that checks HMAC-SHA256 audit chain integrity, external anchors, and access logs."""

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.anchor import DEFAULT_ANCHOR_DIR, get_latest_anchor_manifest
from app.audit.verifier import verify_access_log, verify_chain
from app.db.models import AuditChainHead
from app.db.session import SessionLocal
from app.domain.enums import VerificationRunType


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify audit chain cryptographic integrity, external anchors, and access logs."
    )
    parser.add_argument(
        "--chain",
        default="all",
        help="Chain key to verify (e.g. 'all', 'system', or 'claim:<uuid>')",
    )
    parser.add_argument("--anchor", help="Explicit expected external anchor hash")
    parser.add_argument(
        "--anchor-dir",
        default=DEFAULT_ANCHOR_DIR,
        help="Directory containing daily anchor manifests",
    )
    parser.add_argument(
        "--no-anchor",
        action="store_true",
        help="Disable automatic external anchor manifest checks",
    )
    parser.add_argument(
        "--check-access-log",
        action="store_true",
        default=True,
        help="Verify HMAC integrity of access_log rows (default: True)",
    )
    args = parser.parse_args()

    db: Session = SessionLocal()
    has_error = False

    try:
        manifest = None
        if not args.no_anchor:
            manifest = get_latest_anchor_manifest(args.anchor_dir)
            if manifest:
                print(
                    f"⚓ Loaded latest anchor manifest dated {manifest.get('manifest_date')} (root_hash: {manifest.get('root_hash')[:12]}...)"
                )
            else:
                print("ℹ️ No external anchor manifest found. Verifying internal chain linkage only.")

        # 1. Audit Chain Verification
        if args.chain == "all":
            heads = list(db.scalars(select(AuditChainHead)).all())
            if not heads:
                print("No audit chains found.")
            else:
                print(f"Verifying {len(heads)} audit chains...")
                for head in heads:
                    res = verify_chain(
                        db,
                        head.chain_key,
                        record_run=True,
                        run_type=VerificationRunType.FULL,
                        external_anchor_hash=args.anchor,
                        anchor_manifest=manifest,
                        verify_anchor_manifest=(not args.no_anchor),
                    )
                    if not res.is_valid:
                        print(
                            f"❌ FAIL: Chain '{res.chain_key}' broken at seq {res.first_broken_seq}: {res.error_message}"
                        )
                        has_error = True
                    else:
                        print(
                            f"✅ PASS: Chain '{res.chain_key}' verified ({res.total_events} events)"
                        )
        else:
            res = verify_chain(
                db,
                args.chain,
                record_run=True,
                run_type=VerificationRunType.FULL,
                external_anchor_hash=args.anchor,
                anchor_manifest=manifest,
                verify_anchor_manifest=(not args.no_anchor),
            )
            if not res.is_valid:
                print(
                    f"❌ FAIL: Chain '{res.chain_key}' broken at seq {res.first_broken_seq}: {res.error_message}"
                )
                has_error = True
            else:
                print(f"✅ PASS: Chain '{res.chain_key}' verified ({res.total_events} events)")

        # 2. Access Log Verification
        if args.check_access_log:
            acc_valid, acc_count, acc_err = verify_access_log(db, manifest=manifest)
            if not acc_valid:
                print(f"❌ FAIL: Access log verification failed: {acc_err}")
                has_error = True
            else:
                print(f"✅ PASS: Access log verified ({acc_count} rows checked)")

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

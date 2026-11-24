"""CLI script for generating daily audit anchor manifests (Phase P3 Task 8 & 10)."""

import argparse
import sys

from app.audit.anchor import DEFAULT_ANCHOR_DIR, create_daily_anchor_manifest
from app.db.session import SessionLocal


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate daily cryptographic anchor manifest for audit chains and access logs."
    )
    parser.add_argument(
        "--anchor-dir",
        default=DEFAULT_ANCHOR_DIR,
        help="Optional local directory to write anchor manifests (default: None, writes to D3 S3 Object Lock)",
    )
    parser.add_argument(
        "--date",
        dest="manifest_date",
        default=None,
        help="Explicit manifest date in YYYY-MM-DD format (defaults to UTC today)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        manifest = create_daily_anchor_manifest(
            db=db,
            anchor_dir=args.anchor_dir,
            manifest_date=args.manifest_date,
        )
        print(f"✅ Daily anchor manifest created for date {manifest['manifest_date']}:")
        print(f"   Root Hash: {manifest['root_hash']}")
        print(f"   Chains Anchored: {len(manifest['chain_heads'])}")
        print(f"   Access Log Rows: {manifest['access_log']['count']}")
        print(f"   Watermark: {manifest['watermark']}")
    except Exception as exc:
        print(f"❌ Failed to create anchor manifest: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

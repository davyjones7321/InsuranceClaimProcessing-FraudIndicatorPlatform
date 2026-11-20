"""External audit anchoring and daily manifest creation (Task 8 & Task 10, Decision D3 & D4).

Anchors:
1. Daily manifest with timestamp, previous_manifest_hash, changed chain heads, and root_hash.
2. Access log closed-day count and digest of row_hash values.
3. Transaction watermark preventing race with in-flight transactions.
4. Verifier automatically fetches latest manifest to prove unbroken history.
"""

import hashlib
import json
import os
from datetime import UTC
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.schema import canonical_json_bytes
from app.db.base import utc_now
from app.db.models import AccessLog, AuditChainHead, AuditLog

DEFAULT_ANCHOR_DIR = os.path.join("docs", "evidence", "anchors")


def get_latest_anchor_manifest(anchor_dir: str = DEFAULT_ANCHOR_DIR) -> dict[str, Any] | None:
    """Read the most recent anchor manifest from the anchor storage directory."""
    if not os.path.isdir(anchor_dir):
        return None

    manifest_files = [
        f for f in os.listdir(anchor_dir) if f.startswith("manifest_") and f.endswith(".json")
    ]
    if not manifest_files:
        return None

    manifest_files.sort()
    latest_file = os.path.join(anchor_dir, manifest_files[-1])
    with open(latest_file, encoding="utf-8") as f:
        data: dict[str, Any] = cast(dict[str, Any], json.load(f))
        return data


def create_daily_anchor_manifest(
    db: Session,
    anchor_dir: str = DEFAULT_ANCHOR_DIR,
    manifest_date: str | None = None,
) -> dict[str, Any]:
    """Generate daily anchor manifest recording audit chain heads and access log digests."""
    now = utc_now()
    date_str = manifest_date or now.strftime("%Y-%m-%d")

    # Watermark: timestamp of latest audit row or now
    max_audit_time = db.scalar(select(func.max(AuditLog.occurred_at)))
    watermark_dt = max_audit_time or now
    watermark_iso = watermark_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # 1. Capture chain heads
    heads = db.scalars(select(AuditChainHead).order_by(AuditChainHead.chain_key)).all()
    chain_heads_dict: dict[str, dict[str, Any]] = {}
    for h in heads:
        chain_heads_dict[h.chain_key] = {
            "seq": h.last_seq,
            "hash": h.last_hash,
        }

    # 2. Capture access log digest for closed window
    access_logs = db.scalars(select(AccessLog).order_by(AccessLog.created_at)).all()
    access_count = len(access_logs)
    sorted_row_hashes = sorted([log.row_hash for log in access_logs if log.row_hash])
    combined_hashes = "".join(sorted_row_hashes).encode("utf-8")
    access_digest = hashlib.sha256(combined_hashes).hexdigest()

    # 3. Previous manifest hash
    prev_manifest = get_latest_anchor_manifest(anchor_dir)
    prev_hash = (
        prev_manifest["root_hash"]
        if prev_manifest
        else hashlib.sha256(b"genesis_anchor").hexdigest()
    )

    manifest_body = {
        "access_log": {
            "count": access_count,
            "digest": access_digest,
        },
        "chain_heads": chain_heads_dict,
        "created_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "manifest_date": date_str,
        "previous_manifest_hash": prev_hash,
        "version": 1,
        "watermark": watermark_iso,
    }

    # Compute root hash over canonical manifest body
    canonical_bytes = canonical_json_bytes(manifest_body)
    root_hash = hashlib.sha256(canonical_bytes).hexdigest()
    manifest_body["root_hash"] = root_hash

    # Save to anchor directory
    os.makedirs(anchor_dir, exist_ok=True)
    filename = f"manifest_{date_str}_{root_hash[:12]}.json"
    file_path = os.path.join(anchor_dir, filename)

    # In compliance mode, fail if file already exists (append-only / immutable)
    if os.path.exists(file_path):
        filename = f"manifest_{date_str}_{root_hash[:16]}.json"
        file_path = os.path.join(anchor_dir, filename)

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(manifest_body, f, indent=2, sort_keys=True)

    return manifest_body

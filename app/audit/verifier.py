"""Audit chain verifier that validates sequence gaps, hash linkage, head consistency, and external anchors (Phase P3)."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.anchor import get_latest_anchor_manifest
from app.audit.writer import compute_audit_hash, compute_genesis_hash
from app.db.base import utc_now
from app.db.models import AuditChainHead, AuditLog, VerificationRun
from app.domain.enums import VerificationRunType
from app.security.access_log import compute_access_log_hash


@dataclass(frozen=True)
class ChainVerificationResult:
    chain_key: str
    is_valid: bool
    total_events: int
    first_broken_seq: int | None
    error_message: str | None


def verify_chain(
    db: Session,
    chain_key: str,
    record_run: bool = True,
    run_type: VerificationRunType = VerificationRunType.FULL,
    external_anchor_hash: str | None = None,
    anchor_manifest: dict[str, Any] | None = None,
    verify_anchor_manifest: bool = True,
) -> ChainVerificationResult:
    """Walk an audit chain and verify cryptographic integrity, sequence gaps, head consistency, and anchor linkage."""
    # 1. Check chain head cache
    stmt_head = select(AuditChainHead).where(AuditChainHead.chain_key == chain_key)
    head = db.scalars(stmt_head).first()

    # 2. Fetch all rows ordered by seq
    stmt_rows = select(AuditLog).where(AuditLog.chain_key == chain_key).order_by(AuditLog.seq.asc())
    rows = list(db.scalars(stmt_rows).all())

    if not rows:
        if head and head.last_seq != 0:
            msg = f"Chain head reports last_seq={head.last_seq} but no audit rows exist"
            if record_run:
                _record_verification_run(
                    db, run_type, chain_key, False, 1, msg, external_anchor_hash
                )
            return ChainVerificationResult(chain_key, False, 0, 1, msg)
        return ChainVerificationResult(chain_key, True, 0, None, None)

    genesis_hash = compute_genesis_hash(chain_key)
    expected_prev = genesis_hash

    for expected_seq, row in enumerate(rows, start=1):
        # 1. Sequence gap check
        if row.seq != expected_seq:
            msg = f"Sequence gap detected: expected seq {expected_seq}, found {row.seq}"
            if record_run:
                _record_verification_run(
                    db, run_type, chain_key, False, expected_seq, msg, external_anchor_hash
                )
            return ChainVerificationResult(chain_key, False, len(rows), expected_seq, msg)

        # 2. Genesis / prev_seq constraint check
        if row.seq == 1:
            if row.prev_seq is not None:
                msg = f"Genesis row seq 1 must have prev_seq=None, found {row.prev_seq}"
                if record_run:
                    _record_verification_run(
                        db, run_type, chain_key, False, row.seq, msg, external_anchor_hash
                    )
                return ChainVerificationResult(chain_key, False, len(rows), row.seq, msg)
        else:
            if row.prev_seq != expected_seq - 1:
                msg = f"Broken prev_seq at seq {row.seq}: expected {expected_seq - 1}, found {row.prev_seq}"
                if record_run:
                    _record_verification_run(
                        db, run_type, chain_key, False, row.seq, msg, external_anchor_hash
                    )
                return ChainVerificationResult(chain_key, False, len(rows), row.seq, msg)

        # 3. prev_hash linkage check
        if row.prev_hash != expected_prev:
            msg = f"Linkage broken at seq {row.seq}: expected prev_hash {expected_prev}, found {row.prev_hash}"
            if record_run:
                _record_verification_run(
                    db, run_type, chain_key, False, row.seq, msg, external_anchor_hash
                )
            return ChainVerificationResult(chain_key, False, len(rows), row.seq, msg)

        # 4. Check payload canonical consistency and recompute HMAC-SHA256
        if row.event_data is not None:
            try:
                from app.audit.schema import canonical_json_bytes, validate_and_sanitize_audit_event

                expected_canonical = canonical_json_bytes(
                    validate_and_sanitize_audit_event(row.action, row.event_data)
                ).decode("utf-8")
                if expected_canonical != row.payload_canonical:
                    msg = f"Hash mismatch at seq {row.seq}: event_data does not match payload_canonical"
                    if record_run:
                        _record_verification_run(
                            db, run_type, chain_key, False, row.seq, msg, external_anchor_hash
                        )
                    return ChainVerificationResult(chain_key, False, len(rows), row.seq, msg)
            except Exception:
                msg = f"Hash mismatch at seq {row.seq}: event_data validation failed"
                if record_run:
                    _record_verification_run(
                        db, run_type, chain_key, False, row.seq, msg, external_anchor_hash
                    )
                return ChainVerificationResult(chain_key, False, len(rows), row.seq, msg)

        recomputed_hash = compute_audit_hash(
            chain_key=row.chain_key,
            seq=row.seq,
            occurred_at=row.occurred_at,
            actor_id=row.actor_id,
            actor_role=row.actor_role,
            action=row.action,
            entity_type=row.entity_table,
            entity_id=row.entity_pk,
            claim_id=row.claim_id,
            event_data=row.event_data,
            prev_hash=row.prev_hash,
            request_id=row.request_id,
            ip=row.ip,
            entity_table=row.entity_table,
            op=row.op,
            status_after=row.status_after,
            hash_version=row.hash_version,
            key_id=row.key_id,
            event_id=row.event_id,
            payload_canonical_str=row.payload_canonical,
        )

        if recomputed_hash != row.hash:
            msg = f"Hash mismatch at seq {row.seq}: stored {row.hash}, computed {recomputed_hash}"
            if record_run:
                _record_verification_run(
                    db, run_type, chain_key, False, row.seq, msg, external_anchor_hash
                )
            return ChainVerificationResult(chain_key, False, len(rows), row.seq, msg)

        expected_prev = row.hash

    # 5. Check head consistency
    last_row = rows[-1]
    if head:
        if head.last_seq != last_row.seq:
            msg = (
                f"Head inconsistency: head.last_seq={head.last_seq} != last_row.seq={last_row.seq}"
            )
            if record_run:
                _record_verification_run(
                    db, run_type, chain_key, False, last_row.seq, msg, external_anchor_hash
                )
            return ChainVerificationResult(chain_key, False, len(rows), last_row.seq, msg)

        if head.last_hash != last_row.hash:
            msg = f"Head inconsistency: head.last_hash={head.last_hash} != last_row.hash={last_row.hash}"
            if record_run:
                _record_verification_run(
                    db, run_type, chain_key, False, last_row.seq, msg, external_anchor_hash
                )
            return ChainVerificationResult(chain_key, False, len(rows), last_row.seq, msg)

    # 6. Check external anchor hash if explicitly passed
    if external_anchor_hash and external_anchor_hash != last_row.hash:
        msg = f"External anchor mismatch: expected {external_anchor_hash}, actual last_hash {last_row.hash}"
        if record_run:
            _record_verification_run(
                db, run_type, chain_key, False, last_row.seq, msg, external_anchor_hash
            )
        return ChainVerificationResult(chain_key, False, len(rows), last_row.seq, msg)

    # 7. Check external anchor manifest automatically per Task 8
    if verify_anchor_manifest:
        manifest = anchor_manifest or get_latest_anchor_manifest()
        if manifest and "chain_heads" in manifest:
            anchored_heads = manifest["chain_heads"]
            if chain_key in anchored_heads:
                anchored_info = anchored_heads[chain_key]
                anchored_seq = anchored_info["seq"]
                anchored_hash = anchored_info["hash"]

                # Tail truncation check
                if last_row.seq < anchored_seq:
                    msg = (
                        f"Tail truncation detected via external anchor: anchored seq={anchored_seq}, "
                        f"current chain head seq={last_row.seq}"
                    )
                    if record_run:
                        _record_verification_run(
                            db, run_type, chain_key, False, last_row.seq, msg, anchored_hash
                        )
                    return ChainVerificationResult(chain_key, False, len(rows), last_row.seq, msg)

                # Check that anchored row exists and hash matches
                anchored_row = next((r for r in rows if r.seq == anchored_seq), None)
                if not anchored_row:
                    msg = f"External anchor row missing: anchored seq={anchored_seq} not found"
                    if record_run:
                        _record_verification_run(
                            db, run_type, chain_key, False, anchored_seq, msg, anchored_hash
                        )
                    return ChainVerificationResult(chain_key, False, len(rows), anchored_seq, msg)

                if anchored_row.hash != anchored_hash:
                    msg = (
                        f"External anchor mismatch at seq {anchored_seq}: anchored hash={anchored_hash}, "
                        f"actual hash={anchored_row.hash}"
                    )
                    if record_run:
                        _record_verification_run(
                            db, run_type, chain_key, False, anchored_seq, msg, anchored_hash
                        )
                    return ChainVerificationResult(chain_key, False, len(rows), anchored_seq, msg)

    if record_run:
        _record_verification_run(db, run_type, chain_key, True, None, None, external_anchor_hash)

    return ChainVerificationResult(chain_key, True, len(rows), None, None)


def verify_access_log(
    db: Session,
    manifest: dict[str, Any] | None = None,
) -> tuple[bool, int, str | None]:
    """Verify HMAC integrity of access_log rows and optionally match against daily anchor digest (Task 10 [D4])."""
    from app.db.models import AccessLog

    logs = db.scalars(select(AccessLog).order_by(AccessLog.created_at)).all()
    for idx, entry in enumerate(logs, start=1):
        recomputed = compute_access_log_hash(
            log_id=entry.id,
            actor_id=entry.actor_id,
            actor_role=entry.actor_role,
            claim_id=entry.claim_id,
            document_id=entry.document_id,
            resource_type=entry.resource_type,
            action=entry.action,
            created_at=entry.created_at,
        )
        if entry.row_hash != recomputed:
            return (
                False,
                idx,
                f"Access log row hash mismatch at id {entry.id}: stored {entry.row_hash}, computed {recomputed}",
            )

    if manifest and "access_log" in manifest:
        import hashlib

        expected_count = manifest["access_log"]["count"]
        expected_digest = manifest["access_log"]["digest"]
        if len(logs) < expected_count:
            return (
                False,
                len(logs),
                f"Access log truncation: expected at least {expected_count} rows, found {len(logs)}",
            )

        sorted_hashes = sorted([log.row_hash for log in logs if log.row_hash])
        computed_digest = hashlib.sha256("".join(sorted_hashes).encode("utf-8")).hexdigest()
        if len(logs) == expected_count and computed_digest != expected_digest:
            return (
                False,
                len(logs),
                f"Access log digest mismatch: expected {expected_digest}, computed {computed_digest}",
            )

    return True, len(logs), None


def _record_verification_run(
    db: Session,
    run_type: VerificationRunType,
    chain_key: str,
    is_valid: bool,
    first_broken_seq: int | None,
    error_message: str | None,
    external_anchor_hash: str | None,
) -> None:
    """Record verification run outcome in verification_runs table."""
    v_run = VerificationRun(
        run_type=run_type,
        chain_key=chain_key,
        is_valid=is_valid,
        first_broken_seq=first_broken_seq,
        error_message=error_message,
        external_anchor_hash=external_anchor_hash,
        checked_at=utc_now(),
    )
    db.add(v_run)
    db.flush()

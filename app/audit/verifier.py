"""Audit chain verifier that validates sequence gaps, hash linkage, and head consistency."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import compute_audit_hash, compute_genesis_hash
from app.db.base import utc_now
from app.db.models import AuditChainHead, AuditLog, VerificationRun
from app.domain.enums import VerificationRunType


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
) -> ChainVerificationResult:
    """Walk an independent audit chain and verify cryptographic integrity, sequence gaps, and head consistency."""
    # 1. Check chain head
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
        # Check sequence gap
        if row.seq != expected_seq:
            msg = f"Sequence gap detected: expected seq {expected_seq}, found {row.seq}"
            if record_run:
                _record_verification_run(
                    db, run_type, chain_key, False, expected_seq, msg, external_anchor_hash
                )
            return ChainVerificationResult(chain_key, False, len(rows), expected_seq, msg)

        # Check prev_hash linkage
        if row.prev_hash != expected_prev:
            msg = f"Linkage broken at seq {row.seq}: expected prev_hash {expected_prev}, found {row.prev_hash}"
            if record_run:
                _record_verification_run(
                    db, run_type, chain_key, False, row.seq, msg, external_anchor_hash
                )
            return ChainVerificationResult(chain_key, False, len(rows), row.seq, msg)

        # Recompute HMAC-SHA256
        recomputed_hash = compute_audit_hash(
            chain_key=row.chain_key,
            seq=row.seq,
            occurred_at=row.occurred_at,
            actor_id=row.actor_id,
            actor_role=row.actor_role,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            claim_id=row.claim_id,
            event_data=row.event_data,
            prev_hash=row.prev_hash,
            request_id=row.request_id,
            ip=row.ip,
        )

        if recomputed_hash != row.hash:
            msg = f"Hash mismatch at seq {row.seq}: stored {row.hash}, computed {recomputed_hash}"
            if record_run:
                _record_verification_run(
                    db, run_type, chain_key, False, row.seq, msg, external_anchor_hash
                )
            return ChainVerificationResult(chain_key, False, len(rows), row.seq, msg)

        expected_prev = row.hash

    # Check head consistency
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

    # Check external anchor if provided
    if external_anchor_hash and external_anchor_hash != last_row.hash:
        msg = f"External anchor mismatch: expected {external_anchor_hash}, actual last_hash {last_row.hash}"
        if record_run:
            _record_verification_run(
                db, run_type, chain_key, False, last_row.seq, msg, external_anchor_hash
            )
        return ChainVerificationResult(chain_key, False, len(rows), last_row.seq, msg)

    if record_run:
        _record_verification_run(db, run_type, chain_key, True, None, None, external_anchor_hash)

    return ChainVerificationResult(chain_key, True, len(rows), None, None)


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

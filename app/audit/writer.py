"""Audit log writer with length-prefixed HMAC-SHA256 hash chains and strict allowlists (Phase P3).

Conforms to Appendix B and Decision D4:
- Length-prefixed framing over explicit fields
- Single write path with advisory locks and concurrency serialization
- Strict per-action allowlists and PII / float rejection
- Per-claim, per-customer, per-policy, per-user, and system chains
"""

import hashlib
import hmac
import threading
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit.schema import (
    FloatNotAllowedError,
    SensitiveFieldAuditError,
    canonical_json_bytes,
    validate_and_sanitize_audit_event,
)
from app.config import settings
from app.db.base import utc_now
from app.db.models import AuditChainHead, AuditLog

__all__ = [
    "FloatNotAllowedError",
    "SensitiveFieldAuditError",
    "canonical_json",
    "compute_audit_hash",
    "compute_genesis_hash",
    "get_chain_lock",
    "write_audit_log",
]


def canonical_json(data: dict[str, Any]) -> str:
    """Format dictionary as canonical JSON string."""
    return canonical_json_bytes(data).decode("utf-8")


# In-process per-chain re-entrant lock for SQLite and non-Postgres environments
_CHAIN_LOCKS: dict[str, threading.RLock] = defaultdict(threading.RLock)
_CHAIN_LOCKS_MUTEX = threading.Lock()


def get_chain_lock(chain_key: str) -> threading.RLock:
    """Get or create process-level lock for a given chain key."""
    with _CHAIN_LOCKS_MUTEX:
        return _CHAIN_LOCKS[chain_key]


def compute_genesis_hash(chain_key: str) -> str:
    """Compute deterministic genesis hash for a chain: sha256('genesis:' + chain_key)."""
    return hashlib.sha256(f"genesis:{chain_key}".encode()).hexdigest()


def chain_key_to_advisory_id(chain_key: str) -> int:
    """Convert chain key to a 64-bit signed integer for PostgreSQL pg_advisory_xact_lock."""
    digest = hashlib.sha256(chain_key.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def _frame_part(val: str | bytes) -> bytes:
    """Format an element as length-prefixed bytes: <len>:<value>."""
    if isinstance(val, str):
        b = val.encode("utf-8")
    else:
        b = val
    return f"{len(b)}:".encode("ascii") + b


def build_audit_framed_bytes(
    domain_separator: str,
    hash_version: int,
    key_id: int,
    chain_key: str,
    seq: int,
    prev_hash: str,
    event_id: uuid.UUID,
    occurred_at: datetime,
    actor_id: uuid.UUID | None,
    actor_role: str,
    entity_table: str,
    entity_pk: str,
    op: str,
    action: str,
    status_after: str | None,
    payload_sha256: str,
) -> bytes:
    """Build canonical length-prefixed bytes for HMAC calculation per Appendix B."""
    dt_utc = occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=UTC)
    occurred_at_iso = dt_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    parts = [
        _frame_part(domain_separator),
        _frame_part(str(hash_version)),
        _frame_part(str(key_id)),
        _frame_part(chain_key),
        _frame_part(str(seq)),
        _frame_part(prev_hash),
        _frame_part(str(event_id).lower()),
        _frame_part(occurred_at_iso),
        _frame_part(str(actor_id).lower() if actor_id else ""),
        _frame_part(actor_role),
        _frame_part(entity_table),
        _frame_part(entity_pk),
        _frame_part(op),
        _frame_part(action),
        _frame_part(status_after or ""),
        _frame_part(payload_sha256),
    ]
    return b"".join(parts)


def compute_audit_hash(
    chain_key: str,
    seq: int,
    occurred_at: datetime,
    actor_id: uuid.UUID | None,
    actor_role: str,
    action: str,
    entity_type: str,
    entity_id: str,
    claim_id: uuid.UUID | None = None,
    event_data: dict[str, Any] | None = None,
    prev_hash: str = "",
    request_id: str | None = None,
    ip: str | None = None,
    entity_table: str | None = None,
    op: str = "INSERT",
    status_after: str | None = None,
    hash_version: int = 1,
    key_id: int = 1,
    event_id: uuid.UUID | None = None,
    payload_canonical_str: str | None = None,
    hmac_key_override: str | None = None,
) -> str:
    """Compute HMAC-SHA256 with length-prefixed framing per Appendix B."""
    table_name = entity_table or _derive_table_name(entity_type)
    actual_event_id = event_id or uuid.uuid4()

    if payload_canonical_str is None:
        raw_data = event_data or {}
        try:
            sanitized = validate_and_sanitize_audit_event(action, raw_data)
        except Exception:
            from app.audit.schema import canonical_json_bytes, validate_scalar_value

            sanitized = {str(k): validate_scalar_value(v, str(k)) for k, v in raw_data.items()}
        payload_bytes = canonical_json_bytes(sanitized)
    else:
        payload_bytes = payload_canonical_str.encode("utf-8")

    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    domain_sep = f"AUDIT_V1:{settings.ENV}"

    framed_bytes = build_audit_framed_bytes(
        domain_separator=domain_sep,
        hash_version=hash_version,
        key_id=key_id,
        chain_key=chain_key,
        seq=seq,
        prev_hash=prev_hash,
        event_id=actual_event_id,
        occurred_at=occurred_at,
        actor_id=actor_id,
        actor_role=actor_role,
        entity_table=table_name,
        entity_pk=str(entity_id),
        op=op,
        action=action,
        status_after=status_after,
        payload_sha256=payload_sha256,
    )

    key_str = hmac_key_override or settings.AUDIT_HMAC_KEY
    return hmac.new(key_str.encode("utf-8"), framed_bytes, hashlib.sha256).hexdigest()


def _derive_chain_key(
    entity_type: str,
    entity_id: str,
    claim_id: uuid.UUID | None = None,
) -> str:
    """Derive appropriate chain key per Task 4: claim, customer, policy, user, or system."""
    if claim_id:
        return f"claim:{claim_id}"

    entity_lower = entity_type.lower()
    if entity_lower in ("claim", "claimdocument", "assessment", "decision", "investigation"):
        return f"claim:{entity_id}"
    if entity_lower == "customer":
        return f"customer:{entity_id}"
    if entity_lower in ("policy", "coverage", "policyexclusion"):
        return f"policy:{entity_id}"
    if entity_lower == "user":
        return f"user:{entity_id}"

    return "system"


def _derive_table_name(entity_type: str) -> str:
    """Derive database table name from entity type string."""
    mapping = {
        "claim": "claims",
        "claimdocument": "claim_documents",
        "assessment": "assessments",
        "decision": "decisions",
        "investigation": "investigations",
        "investigationfinding": "investigation_findings",
        "customer": "customers",
        "policy": "policies",
        "coverage": "coverages",
        "policyexclusion": "policy_exclusions",
        "user": "users",
        "requireddocumenttype": "required_document_types",
        "batchimport": "batch_imports",
        "ruleset": "rulesets",
        "accesslog": "access_log",
        "system": "system",
    }
    return mapping.get(entity_type.lower(), entity_type.lower())


def write_audit_log(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: str,
    actor_role: str,
    actor_id: uuid.UUID | None = None,
    claim_id: uuid.UUID | None = None,
    event_data: dict[str, Any] | None = None,
    op: str = "INSERT",
    status_after: str | None = None,
    entity_table: str | None = None,
    request_id: str | None = None,
    ip: str | None = None,
    hash_version: int = 1,
    key_id: int = 1,
) -> AuditLog:
    """Write an append-only audit log entry with strict allowlisting, advisory locking, and HMAC framing."""
    chain_key = _derive_chain_key(entity_type, entity_id, claim_id)
    table_name = entity_table or _derive_table_name(entity_type)
    event_id = uuid.uuid4()
    occurred_at = utc_now()

    # 1. Validate payload against strict action schema (Fix F9)
    sanitized_event_data = validate_and_sanitize_audit_event(action, event_data)
    payload_canonical_bytes = canonical_json_bytes(sanitized_event_data)
    payload_canonical_str = payload_canonical_bytes.decode("utf-8")

    # 2. Acquire transaction-scoped advisory lock or process-level chain lock (Fix F2)
    bind = db.get_bind()
    is_pg = bind is not None and bind.dialect.name == "postgresql"

    # Process lock ensures thread safety in SQLite unit tests
    chain_lock = get_chain_lock(chain_key)
    chain_lock.acquire()
    try:
        if is_pg:
            lock_id = chain_key_to_advisory_id(chain_key)
            db.execute(text("SELECT pg_advisory_xact_lock(:lid)"), {"lid": lock_id})

        # 3. Authoritatively query head row from AuditLog under lock
        latest_audit_stmt = (
            select(AuditLog.seq, AuditLog.hash)
            .where(AuditLog.chain_key == chain_key)
            .order_by(AuditLog.seq.desc())
            .limit(1)
        )
        if is_pg:
            latest_audit_stmt = latest_audit_stmt.with_for_update()

        last_row = db.execute(latest_audit_stmt).first()

        if not last_row:
            new_seq = 1
            prev_seq = None
            prev_hash = compute_genesis_hash(chain_key)
        else:
            new_seq = last_row.seq + 1
            prev_seq = last_row.seq
            prev_hash = last_row.hash

        # 4. Compute length-prefixed HMAC-SHA256
        new_hash = compute_audit_hash(
            chain_key=chain_key,
            seq=new_seq,
            occurred_at=occurred_at,
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            claim_id=claim_id,
            event_data=sanitized_event_data,
            prev_hash=prev_hash,
            request_id=request_id,
            ip=ip,
            entity_table=table_name,
            op=op,
            status_after=status_after,
            hash_version=hash_version,
            key_id=key_id,
            event_id=event_id,
            payload_canonical_str=payload_canonical_str,
        )

        # 5. Insert AuditLog row
        audit_entry = AuditLog(
            chain_key=chain_key,
            seq=new_seq,
            prev_seq=prev_seq,
            prev_hash=prev_hash,
            hash=new_hash,
            hash_version=hash_version,
            key_id=key_id,
            event_id=event_id,
            occurred_at=occurred_at,
            actor_id=actor_id,
            actor_role=actor_role,
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_table=table_name,
            entity_pk=str(entity_id),
            op=op,
            action=action,
            status_after=status_after,
            payload_canonical=payload_canonical_str,
            event_data=sanitized_event_data,
            claim_id=claim_id,
            request_id=request_id,
            ip=ip,
        )
        db.add(audit_entry)

        # 6. Update or insert AuditChainHead cache
        head_stmt = select(AuditChainHead).where(AuditChainHead.chain_key == chain_key)
        if is_pg:
            head_stmt = head_stmt.with_for_update()
        head = db.scalars(head_stmt).first()
        if not head:
            head = AuditChainHead(
                chain_key=chain_key,
                last_seq=new_seq,
                last_hash=new_hash,
                updated_at=occurred_at,
            )
            db.add(head)
        else:
            head.last_seq = new_seq
            head.last_hash = new_hash
            head.updated_at = occurred_at

        db.flush()
        return audit_entry
    finally:
        chain_lock.release()

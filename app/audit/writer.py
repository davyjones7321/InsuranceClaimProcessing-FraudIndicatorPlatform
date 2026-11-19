"""Audit log writer with HMAC-SHA256 independent hash chains and PII allowlisting."""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.base import utc_now
from app.db.models import AuditChainHead, AuditLog

# Disallowed PII / sensitive field names
DISALLOWED_SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "password_hash",
        "dob",
        "date_of_birth",
        "phone",
        "phone_number",
        "address",
        "ssn",
        "national_id",
        "aadhaar",
        "medical_content",
        "medical_records",
        "diagnosis",
        "document_content",
        "file_bytes",
        "raw_data",
    }
)


class FloatNotAllowedError(TypeError):
    """Raised when a float is passed into canonical JSON serialization."""

    pass


class SensitiveFieldAuditError(ValueError):
    """Raised when sensitive PII data is passed to audit serialization."""

    pass


def compute_genesis_hash(chain_key: str) -> str:
    """Compute deterministic genesis hash for a chain: sha256('genesis:' + chain_key)."""
    return hashlib.sha256(f"genesis:{chain_key}".encode()).hexdigest()


def _sanitize_and_filter_event_data(data: Any, path: str = "") -> Any:
    """Validate and filter event data against sensitive fields, rejecting floats and raw PII."""
    if isinstance(data, float):
        raise FloatNotAllowedError(f"Floats are strictly rejected in audit event data: {data}")

    if isinstance(data, dict):
        sanitized: dict[str, Any] = {}
        for key, value in data.items():
            key_lower = str(key).lower()
            if key_lower in DISALLOWED_SENSITIVE_FIELDS:
                raise SensitiveFieldAuditError(
                    f"Sensitive field '{key}' is forbidden in audit log. Use allowlisted non-PII values only."
                )
            sanitized[str(key)] = _sanitize_and_filter_event_data(
                value, f"{path}.{key}" if path else str(key)
            )
        return sanitized

    if isinstance(data, list | tuple):
        return [_sanitize_and_filter_event_data(item, f"{path}[]") for item in data]

    if isinstance(data, Decimal):
        # Format Decimal as fixed-scale string with 2 decimal places
        return str(data.quantize(Decimal("0.01")))

    if isinstance(data, datetime):
        # Ensure UTC and ISO-8601 with microseconds and Z
        dt_utc = data if data.tzinfo else data.replace(tzinfo=UTC)
        return dt_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    if isinstance(data, uuid.UUID):
        return str(data).lower()

    return data


def canonical_json(data: dict[str, Any]) -> bytes:
    """Serialize data into canonical JSON: sorted keys, UTF-8, no whitespace, no floats."""
    sanitized = _sanitize_and_filter_event_data(data)
    return json.dumps(
        sanitized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_audit_hash(
    chain_key: str,
    seq: int,
    occurred_at: datetime,
    actor_id: uuid.UUID | None,
    actor_role: str,
    action: str,
    entity_type: str,
    entity_id: str,
    claim_id: uuid.UUID | None,
    event_data: dict[str, Any],
    prev_hash: str,
    request_id: str | None = None,
    ip: str | None = None,
) -> str:
    """Compute HMAC-SHA256 over all event fields except hash itself."""
    payload_dict = {
        "chain_key": chain_key,
        "seq": seq,
        "occurred_at": occurred_at,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "claim_id": claim_id,
        "event_data": event_data,
        "prev_hash": prev_hash,
        "request_id": request_id,
        "ip": ip,
    }
    canonical_bytes = canonical_json(payload_dict)
    key_bytes = settings.AUDIT_HMAC_KEY.encode("utf-8")
    return hmac.new(key_bytes, canonical_bytes, hashlib.sha256).hexdigest()


def write_audit_log(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: str,
    actor_role: str,
    actor_id: uuid.UUID | None = None,
    claim_id: uuid.UUID | None = None,
    event_data: dict[str, Any] | None = None,
    request_id: str | None = None,
    ip: str | None = None,
) -> AuditLog:
    """Write an append-only audit log entry in the current transaction with row-level head locking."""
    # Determine independent chain key: per-claim or system
    chain_key = f"claim:{claim_id}" if claim_id else "system"
    clean_event_data = event_data or {}
    occurred_at = utc_now()

    # Lock chain head row with SELECT ... FOR UPDATE on PostgreSQL
    stmt = select(AuditChainHead).where(AuditChainHead.chain_key == chain_key)
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    chain_head = db.scalars(stmt).first()

    if not chain_head:
        genesis = compute_genesis_hash(chain_key)
        chain_head = AuditChainHead(
            chain_key=chain_key,
            last_seq=0,
            last_hash=genesis,
            updated_at=occurred_at,
        )
        db.add(chain_head)
        db.flush()

    new_seq = chain_head.last_seq + 1
    prev_hash = chain_head.last_hash

    # Compute HMAC-SHA256
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
        event_data=clean_event_data,
        prev_hash=prev_hash,
        request_id=request_id,
        ip=ip,
    )

    # Insert audit row
    audit_entry = AuditLog(
        chain_key=chain_key,
        seq=new_seq,
        occurred_at=occurred_at,
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        claim_id=claim_id,
        event_data=_sanitize_and_filter_event_data(clean_event_data),
        request_id=request_id,
        ip=ip,
        prev_hash=prev_hash,
        hash=new_hash,
    )
    db.add(audit_entry)

    # Advance head
    chain_head.last_seq = new_seq
    chain_head.last_hash = new_hash
    chain_head.updated_at = occurred_at

    db.flush()
    return audit_entry

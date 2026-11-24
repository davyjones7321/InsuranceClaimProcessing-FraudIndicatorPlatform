"""Audit package with HMAC-SHA256 chaining and verification."""

from app.audit.schema import (
    FloatNotAllowedError,
    SensitiveFieldAuditError,
    hash_free_text,
)
from app.audit.verifier import ChainVerificationResult, verify_chain
from app.audit.writer import (
    canonical_json,
    compute_audit_hash,
    compute_genesis_hash,
    write_audit_log,
)

__all__ = [
    "ChainVerificationResult",
    "FloatNotAllowedError",
    "SensitiveFieldAuditError",
    "canonical_json",
    "compute_audit_hash",
    "compute_genesis_hash",
    "hash_free_text",
    "verify_chain",
    "write_audit_log",
]

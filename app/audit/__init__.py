"""Audit package with HMAC-SHA256 chaining and verification."""

from app.audit.verifier import ChainVerificationResult, verify_chain
from app.audit.writer import (
    FloatNotAllowedError,
    SensitiveFieldAuditError,
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
    "verify_chain",
    "write_audit_log",
]

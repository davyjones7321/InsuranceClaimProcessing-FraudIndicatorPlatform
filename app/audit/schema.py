"""Strict schema, allowlists, and canonical formatting for audit events (Task 3, Fix F9).

Enforces:
1. Allowed actions allowlist
2. Per-action allowed keys schema
3. Value type validation (ids, enums, dates, fixed-scale decimal strings, sha256 hashes)
4. Strict rejection of free text (remarks, descriptions, findings, names, comments)
5. Strict rejection of floats (IEEE-754 nondeterminism)
6. Strict rejection of sensitive PII (passwords, medical, phone, email, etc.)
"""

import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

# Disallowed PII field names (case-insensitive substring or exact match)
FORBIDDEN_PII_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "password_hash",
        "hashed_password",
        "dob",
        "date_of_birth",
        "phone",
        "phone_number",
        "address",
        "full_name",
        "first_name",
        "last_name",
        "ssn",
        "national_id",
        "aadhaar",
        "medical_content",
        "medical_records",
        "diagnosis",
        "document_content",
        "file_bytes",
        "raw_data",
        "email",
    }
)

# Forbidden free text field names in audit event payloads
FORBIDDEN_FREE_TEXT_KEYS: frozenset[str] = frozenset(
    {
        "remarks",
        "description",
        "findings",
        "notes",
        "summary",
        "comment",
        "comments",
        "message",
        "free_text",
        "reason_text",
    }
)

# Per-action explicit allowed keys schema
ACTION_SCHEMAS: dict[str, frozenset[str]] = {
    # Authentication & User
    "USER_LOGIN": frozenset({"user_id", "role", "channel"}),
    "LOGIN": frozenset({"user_id", "role", "channel"}),
    "USER_LOGOUT": frozenset({"user_id"}),
    "LOGOUT": frozenset({"user_id"}),
    # Master Data: Customers
    "CUSTOMER_CREATED": frozenset({"customer_id", "customer_no", "state_region"}),
    "CREATE_CUSTOMER": frozenset({"customer_id", "customer_no", "state_region"}),
    "CUSTOMER_UPDATED": frozenset({"customer_id", "updated_fields"}),
    "UPDATE_CUSTOMER": frozenset({"customer_id", "updated_fields"}),
    # Master Data: Policies
    "POLICY_CREATED": frozenset(
        {"policy_id", "policy_no", "product_type", "customer_id", "coverages_count", "sum_insured"}
    ),
    "CREATE_POLICY": frozenset(
        {"policy_id", "policy_no", "product_type", "customer_id", "coverages_count", "sum_insured"}
    ),
    "POLICY_UPDATED": frozenset({"policy_id", "updated_fields", "status"}),
    "UPDATE_POLICY": frozenset({"policy_id", "updated_fields", "status"}),
    "POLICY_STATUS_CHANGE": frozenset({"policy_id", "from_status", "to_status"}),
    # Master Data: Coverages
    "COVERAGE_CREATED": frozenset(
        {
            "coverage_id",
            "policy_id",
            "claim_category",
            "limit_amount",
            "limit_scope",
            "limit_exceeded_behavior",
            "deductible",
            "copay_percent",
            "waiting_period_days",
        }
    ),
    "ADD_COVERAGE": frozenset(
        {
            "coverage_id",
            "policy_id",
            "claim_category",
            "limit_amount",
            "limit_scope",
            "limit_exceeded_behavior",
            "deductible",
            "copay_percent",
            "waiting_period_days",
        }
    ),
    "COVERAGE_UPDATED": frozenset({"coverage_id", "updated_fields"}),
    "UPDATE_COVERAGE": frozenset({"coverage_id", "updated_fields"}),
    # Master Data: Exclusions
    "EXCLUSION_CREATED": frozenset({"policy_id", "exclusion_code"}),
    "ADD_EXCLUSION": frozenset({"policy_id", "exclusion_code"}),
    "EXCLUSION_DELETED": frozenset({"policy_id", "exclusion_code"}),
    "DELETE_EXCLUSION": frozenset({"policy_id", "exclusion_code"}),
    # Master Data: Required Documents
    "REQUIRED_DOC_CREATED": frozenset(
        {"product_type", "claim_category", "doc_code", "is_mandatory"}
    ),
    "CREATE_REQUIRED_DOCUMENT_TYPE": frozenset(
        {"product_type", "claim_category", "doc_code", "is_mandatory"}
    ),
    "REQUIRED_DOC_UPDATED": frozenset({"doc_code", "updated_fields"}),
    "UPDATE_REQUIRED_DOCUMENT_TYPE": frozenset({"doc_code", "updated_fields"}),
    "REQUIRED_DOC_DELETED": frozenset({"doc_code"}),
    "DELETE_REQUIRED_DOCUMENT_TYPE": frozenset({"doc_code"}),
    # Claim Lifecycle
    "CLAIM_REGISTERED": frozenset(
        {
            "claim_ref",
            "requested_amount",
            "channel",
            "claim_category",
            "policy_id",
            "customer_id",
            "note_sha256",
        }
    ),
    "REGISTER": frozenset({"requested_amount", "channel"}),
    "CLAIM_STATUS_CHANGE": frozenset(
        {
            "from_status",
            "to_status",
            "status_version",
            "reason_code",
            "reason_sha256",
            "remarks_sha256",
        }
    ),
    "CLAIM_STATUS_TRANSITION": frozenset(
        {
            "from_status",
            "to_status",
            "status_version",
            "reason_code",
            "reason_sha256",
            "remarks_sha256",
        }
    ),
    "DOCUMENT_UPLOADED": frozenset(
        {
            "doc_code",
            "document_sha256",
            "file_size",
            "sha256",
            "size_bytes",
            "mime_type",
            "file_name_sha256",
            "antivirus_status",
        }
    ),
    "UPLOAD_DOC": frozenset({"doc_code", "file_name", "file_name_sha256", "sha256"}),
    "DOCUMENT_SUPERSEDED": frozenset({"doc_code", "old_document_id", "document_sha256"}),
    "DOCUMENT_REVIEWED": frozenset(
        {"doc_code", "status", "reason_code", "review_status", "remarks_sha256"}
    ),
    "IDENTITY_ATTESTED": frozenset(
        {
            "document_id",
            "rev",
            "prev_rev",
            "document_sha256",
            "result",
            "reason_code",
            "remarks_sha256",
            "reviewer_role",
            "subject_identity_version",
        }
    ),
    "DOCUMENT_IDENTITY_ATTESTED": frozenset(
        {
            "document_id",
            "rev",
            "prev_rev",
            "document_sha256",
            "result",
            "reason_code",
            "remarks_sha256",
            "reviewer_role",
            "subject_identity_version",
        }
    ),
    "SENSITIVE_DOCUMENT_DOWNLOADED": frozenset({"doc_code", "sha256", "file_name_sha256"}),
    "VALIDATION_RUN": frozenset(
        {
            "overall_result",
            "rule_count",
            "failed_count",
            "reason_codes",
            "rule_results",
            "ruleset_version",
            "evaluated_on",
            "failing_rules_count",
            "outcome",
        }
    ),
    "CLAIM_VALIDATED": frozenset(
        {
            "overall_result",
            "rule_count",
            "failed_count",
            "reason_codes",
            "rule_results",
            "ruleset_version",
            "evaluated_on",
            "failing_rules_count",
        }
    ),
    "VALIDATE": frozenset({"overall_result", "rule_count"}),
    "VALIDATION_PASSED": frozenset({"outcome"}),
    # Risk Evaluation & Investigations
    "RISK_EVALUATION": frozenset(
        {
            "category",
            "score",
            "triggered_count",
            "routed_to_investigation",
            "completeness_status",
            "evaluation_status",
            "requires_investigation",
        }
    ),
    "CLAIM_RISK_EVALUATED": frozenset(
        {
            "category",
            "score",
            "triggered_count",
            "routed_to_investigation",
            "completeness",
            "completeness_status",
            "composed_label",
            "evaluation_status",
            "requires_investigation",
        }
    ),
    "INVESTIGATION_CREATED": frozenset({"score", "category", "status", "referral_type"}),
    "INVESTIGATION_ASSIGNED": frozenset({"investigation_id", "assigned_to"}),
    "INVESTIGATION_STARTED": frozenset({"investigation_id", "previous_status"}),
    "INVESTIGATION_UNDER_REVIEW": frozenset({"investigation_id", "previous_status"}),
    "INVESTIGATION_FINDING": frozenset({"investigation_id", "finding_type", "findings_sha256"}),
    "INVESTIGATION_FINDING_ADDED": frozenset(
        {"investigation_id", "finding_type", "findings_sha256"}
    ),
    "INVESTIGATION_ESCALATED": frozenset({"investigation_id", "reason_code", "reason_sha256"}),
    "INVESTIGATION_COMPLETED": frozenset(
        {"investigation_id", "outcome", "findings_count", "remarks_sha256", "summary_sha256"}
    ),
    "INVESTIGATION_RESOLVED": frozenset({"investigation_id", "resolution", "notes_sha256"}),
    "MANUAL_INVESTIGATION_REFERRAL": frozenset(
        {
            "from_status",
            "to_status",
            "status_version",
            "remarks_sha256",
            "referral_type",
            "investigation_id",
        }
    ),
    "RISK_COMPLETENESS_WAIVER": frozenset(
        {
            "from_status",
            "to_status",
            "status_version",
            "remarks_sha256",
        }
    ),
    "INVESTIGATION_FEEDBACK_RECORDED": frozenset({"feedback"}),
    # Incident groups
    "INCIDENT_GROUP_PROPOSED": frozenset(
        {
            "group_ref",
            "customer_id",
            "member_claim_ids",
            "reason_sha256",
        }
    ),
    "CLAIM_INCIDENT_GROUP_PROPOSED": frozenset({"group_id", "group_ref"}),
    "INCIDENT_GROUP_CONFIRMED": frozenset(
        {
            "group_ref",
            "confirmed_by_id",
            "remarks_sha256",
        }
    ),
    "CLAIM_INCIDENT_GROUP_LINKED": frozenset({"group_id", "group_ref"}),
    "INCIDENT_GROUP_CLAIM_UNLINKED": frozenset(
        {
            "group_ref",
            "claim_id",
            "reason",
            "remarks_sha256",
        }
    ),
    "INCIDENT_GROUPS_MERGED": frozenset(
        {
            "target_group_ref",
            "source_group_ref",
            "merged_claim_count",
            "remarks_sha256",
        }
    ),
    "INCIDENT_GROUP_SPLIT": frozenset(
        {
            "source_group_ref",
            "new_group_ref",
            "split_claims_count",
            "remarks_sha256",
        }
    ),
    # Assessment & Decision
    "ASSESSMENT_COMPLETED": frozenset(
        {
            "assessed_loss",
            "eligible_amount",
            "max_eligible_amount",
            "remaining_limit",
            "deductible",
            "copay_percent",
            "remarks_sha256",
        }
    ),
    "CLAIM_ASSESSED": frozenset(
        {
            "assessed_loss",
            "eligible_amount",
            "max_eligible_amount",
            "remaining_limit",
            "deductible",
            "copay_percent",
            "remarks_sha256",
        }
    ),
    "CLAIM_DECISION": frozenset(
        {
            "decision",
            "approved_amount",
            "limit_applied",
            "reason_codes",
            "remarks_sha256",
        }
    ),
    "CLAIM_REOPENED": frozenset(
        {"action", "from_status", "to_status", "previous_status", "target_status", "remarks_sha256"}
    ),
    "REVERSAL": frozenset(
        {"action", "from_status", "to_status", "previous_status", "target_status", "remarks_sha256"}
    ),
    # System & Operations
    "BATCH_IMPORT": frozenset({"batch_type", "record_count", "dry_run", "summary_sha256"}),
    "IMPORT_MASTER_DATA": frozenset(
        {
            "source_format",
            "entity_type",
            "items_created",
            "items_updated",
            "errors_count",
            "batch_id",
            "customers_processed",
            "policies_processed",
            "coverages_processed",
            "claims_processed",
            "rejects_created",
        }
    ),
    "RULESET_ACTIVATED": frozenset({"version", "content_sha256"}),
    "SEED_DATABASE": frozenset(
        {
            "environment",
            "record_counts",
            "customers_count",
            "policies_count",
            "doc_types_count",
        }
    ),
    "ANCHOR_CREATED": frozenset({"manifest_hash", "chain_heads_count", "watermark"}),
    "VERIFICATION_RUN": frozenset(
        {
            "run_type",
            "is_valid",
            "total_events",
            "chain_key",
            "first_broken_seq",
            "external_anchor_hash",
        }
    ),
    "CHAIN_BREAK_ACKNOWLEDGED": frozenset({"chain_key", "broken_seq", "acknowledged_by"}),
    "RETENTION_REPORT": frozenset({"as_of_date", "eligible_records_count"}),
    "BACKUP_DRILL": frozenset({"drill_date", "status", "audit_log_count"}),
    "KEY_ROTATION": frozenset({"old_key_id", "new_key_id"}),
    "SYSTEM_EVENT": frozenset({"component", "event_code", "metric_value", "hash"}),
    # Test actions
    "ACTION_1": frozenset({"key", "step"}),
    "ACTION_2": frozenset({"key", "step"}),
    "ACTION_3": frozenset({"key", "step"}),
    "FLOAT_TEST": frozenset({"bad_float"}),
    "PII_TEST": frozenset({"password"}),
    "PII_TEST_2": frozenset({"dob"}),
}


class AuditSchemaError(ValueError):
    """Base exception for audit schema violations."""

    pass


class UnknownActionError(AuditSchemaError):
    """Raised when an unapproved audit action is specified."""

    pass


class UnknownAuditEventKeyError(AuditSchemaError):
    """Raised when an unknown key is passed for an action schema."""

    pass


class FreeTextAuditError(AuditSchemaError):
    """Raised when free text is passed instead of structured id/enum/hash."""

    pass


class SensitiveFieldAuditError(AuditSchemaError):
    """Raised when PII or sensitive field is detected in audit payload."""

    pass


class FloatNotAllowedError(TypeError, AuditSchemaError):
    """Raised when floats are passed to canonical JSON serialization."""

    pass


HEX_64_REGEX = re.compile(r"^[0-9a-fA-F]{64}$")
UUID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DECIMAL_STRING_REGEX = re.compile(r"^-?\d+(\.\d{1,4})?$")
DATE_STRING_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?Z)?$")
ENUM_IDENTIFIER_REGEX = re.compile(r"^[A-Z0-9_]{1,64}$")


def validate_scalar_value(value: Any, key_name: str) -> Any:
    """Validate that a value conforms to allowed types: id, enum, date, decimal-as-string, hash, int, bool."""
    if isinstance(value, float):
        raise FloatNotAllowedError(
            f"Floats are strictly rejected in audit log (key '{key_name}'): {value}"
        )

    if value is None:
        return None

    if isinstance(value, bool | int):
        return value

    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))

    if isinstance(value, uuid.UUID):
        return str(value).lower()

    if isinstance(value, datetime):
        dt_utc = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, str):
        val_str = value.strip()
        # Reject obvious free-text phrases (> 64 characters unless it is a 64-char sha256 hash)
        if len(val_str) > 64 and not HEX_64_REGEX.match(val_str):
            raise FreeTextAuditError(
                f"Free text is prohibited in audit log (key '{key_name}', length {len(val_str)}): '{val_str[:30]}...'. "
                "Use a SHA-256 hash or allowed enum identifier."
            )

        # Check for personal field name indicators (unless recording field names themselves)
        if not key_name.startswith("updated_fields"):
            for pii in FORBIDDEN_PII_FIELDS:
                if pii in val_str.lower():
                    raise SensitiveFieldAuditError(
                        f"Value for '{key_name}' contains potential PII indicator '{pii}'"
                    )

        return val_str

    if isinstance(value, list | tuple | set):
        return [validate_scalar_value(item, f"{key_name}[]") for item in value]

    if isinstance(value, dict):
        validated_dict: dict[str, Any] = {}
        for sub_k, sub_v in value.items():
            sub_k_str = str(sub_k)
            if sub_k_str.lower() in FORBIDDEN_PII_FIELDS:
                raise SensitiveFieldAuditError(
                    f"Forbidden PII key '{sub_k_str}' detected in nested dictionary"
                )
            if sub_k_str.lower() in FORBIDDEN_FREE_TEXT_KEYS:
                raise FreeTextAuditError(
                    f"Forbidden free-text key '{sub_k_str}' detected in nested dictionary"
                )
            validated_dict[sub_k_str] = validate_scalar_value(sub_v, f"{key_name}.{sub_k_str}")
        return validated_dict

    raise AuditSchemaError(
        f"Unsupported data type '{type(value).__name__}' for audit key '{key_name}'"
    )


def validate_and_sanitize_audit_event(
    action: str, event_data: dict[str, Any] | None
) -> dict[str, Any]:
    """Validate audit event against action schema and sanitize all values into canonical representations."""
    raw_data = event_data or {}

    # 1. PII and Float check across raw data (defense-in-depth)
    for key, value in raw_data.items():
        key_str = str(key)
        if key_str.lower() in FORBIDDEN_PII_FIELDS:
            raise SensitiveFieldAuditError(
                f"Sensitive field '{key_str}' is forbidden in audit log. "
                "Audit logs must never contain personal identifiers."
            )
        if isinstance(value, float):
            raise FloatNotAllowedError(
                f"Floats are strictly rejected in audit log (key '{key_str}'): {value}"
            )

    if action not in ACTION_SCHEMAS:
        raise UnknownActionError(
            f"Action '{action}' is not in the allowed audit actions schema. "
            f"Allowed actions: {sorted(ACTION_SCHEMAS.keys())}"
        )

    allowed_keys = ACTION_SCHEMAS[action]

    sanitized: dict[str, Any] = {}
    for key, value in raw_data.items():
        key_str = str(key)
        key_lower = key_str.lower()

        # 2. Free text key check
        if key_lower in FORBIDDEN_FREE_TEXT_KEYS:
            raise FreeTextAuditError(
                f"Free text key '{key_str}' is forbidden in audit log. "
                f"Store a SHA-256 hash (e.g. '{key_str}_sha256') instead of raw free text."
            )

        # 3. Action schema allowlist check
        if key_str not in allowed_keys:
            raise UnknownAuditEventKeyError(
                f"Key '{key_str}' is not permitted for action '{action}'. "
                f"Allowed keys for '{action}': {sorted(allowed_keys)}"
            )

        # 4. Value validation
        sanitized[key_str] = validate_scalar_value(value, key_str)

    return sanitized


def canonical_json_bytes(data: dict[str, Any]) -> bytes:
    """Serialize sanitized data to canonical JSON (sorted keys, no spaces, UTF-8, no floats)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def hash_free_text(text: str | None) -> str | None:
    """Helper to compute deterministic SHA-256 hex digest of free text (e.g. remarks, findings)."""
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

"""Domain enumeration types."""

from enum import StrEnum


class UserRole(StrEnum):
    CUSTOMER = "customer"
    STAFF = "staff"
    ASSESSOR = "assessor"
    INVESTIGATOR = "investigator"
    MANAGER = "manager"
    ADMIN = "admin"


class ProductType(StrEnum):
    HEALTH = "HEALTH"
    VEHICLE = "VEHICLE"
    PROPERTY = "PROPERTY"


class PolicyStatus(StrEnum):
    ACTIVE = "ACTIVE"
    LAPSED = "LAPSED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"


class LimitScope(StrEnum):
    PER_CLAIM = "PER_CLAIM"
    PER_POLICY_PERIOD = "PER_POLICY_PERIOD"


class CoverageLedgerEntryType(StrEnum):
    CONSUME = "CONSUME"
    REVERSE = "REVERSE"


class ClaimChannel(StrEnum):
    CUSTOMER = "CUSTOMER"
    STAFF = "STAFF"


class ClaimStatus(StrEnum):
    DRAFT = "DRAFT"
    REGISTERED = "REGISTERED"
    DOCUMENTS_PENDING = "DOCUMENTS_PENDING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    VALIDATED = "VALIDATED"
    RISK_DATA_PENDING = "RISK_DATA_PENDING"
    RISK_EVALUATED = "RISK_EVALUATED"
    UNDER_INVESTIGATION = "UNDER_INVESTIGATION"
    ESCALATED = "ESCALATED"
    INVESTIGATION_COMPLETE = "INVESTIGATION_COMPLETE"
    READY_FOR_ASSESSMENT = "READY_FOR_ASSESSMENT"
    ASSESSED = "ASSESSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INFO_REQUESTED = "INFO_REQUESTED"
    WITHDRAWN = "WITHDRAWN"


class DocumentStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    VALID = "VALID"
    INVALID = "INVALID"
    SUPERSEDED = "SUPERSEDED"


class AttestationResult(StrEnum):
    CONFIRMED = "CONFIRMED"
    MISMATCH = "MISMATCH"
    UNCERTAIN = "UNCERTAIN"


class ValidationOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED_DOCS = "BLOCKED_DOCS"


class RuleOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class RiskCategory(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class IndicatorStatus(StrEnum):
    PILOT = "PILOT"
    VALIDATED = "VALIDATED"


class InvestigationStatus(StrEnum):
    ASSIGNED = "ASSIGNED"
    UNDER_REVIEW = "UNDER_REVIEW"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"


class InvestigationOutcome(StrEnum):
    CLEARED = "CLEARED"
    CONCERNS_CONFIRMED = "CONCERNS_CONFIRMED"
    INCONCLUSIVE = "INCONCLUSIVE"


class DecisionType(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INFO_REQUESTED = "INFO_REQUESTED"


class AuditAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    TRANSITION = "TRANSITION"
    DECIDE = "DECIDE"
    REVIEW = "REVIEW"
    INVESTIGATE = "INVESTIGATE"
    REVERSAL = "REVERSAL"
    SYSTEM = "SYSTEM"


class VerificationRunType(StrEnum):
    INCREMENTAL = "INCREMENTAL"
    FULL = "FULL"

"""Pure dataclasses and output types for the deterministic validation and risk engines."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

# Standard platform rounding mode and scale
MONETARY_EXPONENT = Decimal("0.01")


def round_money(amount: Decimal) -> Decimal:
    """Deterministic rounding to two decimal places using ROUND_HALF_UP."""
    return amount.quantize(MONETARY_EXPONENT, rounding=ROUND_HALF_UP)


class ValidationOutcomeEnum(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED_DOCS = "BLOCKED_DOCS"


class RuleOutcomeEnum(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class RiskCategoryEnum(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class ClaimInput:
    claim_id: str
    claim_ref: str
    customer_id: str
    policy_id: str
    claim_category: str
    incident_date: date
    incident_location: str
    incident_description: str
    requested_amount: Decimal
    registered_date: date
    insured_item_id: str | None = None


@dataclass(frozen=True)
class PolicyInput:
    policy_id: str
    policy_no: str
    customer_id: str
    product_type: str
    start_date: date
    end_date: date
    status: str


@dataclass(frozen=True)
class CoverageInput:
    coverage_id: str
    claim_category: str
    limit_amount: Decimal
    consumed_amount: Decimal
    deductible: Decimal
    copay_percent: Decimal
    waiting_period_days: int
    is_excluded: bool
    limit_exceeded_behavior: str = "CAP"
    limit_scope: str = "PER_POLICY_PERIOD"


@dataclass(frozen=True)
class ExclusionInput:
    exclusion_code: str
    description: str


@dataclass(frozen=True)
class DocumentInput:
    document_id: str
    doc_code: str
    status: str  # VALID, INVALID, PENDING_REVIEW, SUPERSEDED
    named_person: str | None = None
    document_date: date | None = None


@dataclass(frozen=True)
class RequiredDocumentTypeInput:
    doc_code: str
    name: str
    is_mandatory: bool


@dataclass(frozen=True)
class ValidationRuleResult:
    rule_id: str
    rule_version: str
    outcome: RuleOutcomeEnum
    reason_code: str | None
    message: str | None
    inputs_snapshot: dict[str, Any]


@dataclass(frozen=True)
class ValidationOutput:
    ruleset_version: str
    engine_version: str
    evaluated_on: date
    overall_result: ValidationOutcomeEnum
    rule_results: list[ValidationRuleResult]
    blocked_document_reasons: list[str]


@dataclass(frozen=True)
class ValidationInput:
    claim: ClaimInput
    policy: PolicyInput | None
    coverages: tuple[CoverageInput, ...]
    exclusions: tuple[ExclusionInput, ...]
    documents: tuple[DocumentInput, ...]
    required_document_types: tuple[RequiredDocumentTypeInput, ...]
    evaluation_date: date
    ruleset: dict[str, Any]


@dataclass(frozen=True)
class HistoricalClaimInput:
    claim_id: str
    claim_ref: str
    customer_id: str
    policy_id: str
    claim_category: str
    incident_date: date
    registered_date: date
    requested_amount: Decimal
    status: str
    approved_amount: Decimal | None = None


@dataclass(frozen=True)
class RiskIndicatorResult:
    indicator_id: str
    indicator_version: str
    triggered: bool
    weight_applied: int
    evidence: dict[str, Any]
    message: str
    status: str  # PILOT, VALIDATED
    skipped: bool = False
    skip_reason: str | None = None


@dataclass(frozen=True)
class RiskOutput:
    ruleset_version: str
    engine_version: str
    evaluated_on: date
    score: int  # 0..100
    category: RiskCategoryEnum  # LOW, MEDIUM, HIGH
    requires_investigation: bool
    indicator_results: list[RiskIndicatorResult]


@dataclass(frozen=True)
class RiskInput:
    claim: ClaimInput
    customer_name: str
    customer_state_region: str | None
    policy: PolicyInput | None
    coverage: CoverageInput | None
    documents: tuple[DocumentInput, ...]
    historical_claims: tuple[HistoricalClaimInput, ...]
    peer_approved_amounts: tuple[Decimal, ...]
    evaluation_date: date
    ruleset: dict[str, Any]


@dataclass(frozen=True)
class AssessmentInput:
    product_type: str  # HEALTH, VEHICLE, PROPERTY
    requested_amount: Decimal
    remaining_limit: Decimal
    assessed_loss: Decimal
    deductible: Decimal = Decimal("0.00")
    copay_percent: Decimal = Decimal("0.00")
    depreciation: Decimal = Decimal("0.00")
    sum_insured: Decimal = Decimal("0.00")
    value_at_risk: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class AssessmentOutput:
    product_type: str
    assessed_loss: Decimal
    max_eligible_amount: Decimal
    calculation_trace: dict[str, Any]

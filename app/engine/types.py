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

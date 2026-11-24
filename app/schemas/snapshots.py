"""Strict allowlist snapshot models and serializers for deterministic replay (P5 Tasks 8 & 9).

Enforces strict exclusion of PII and prohibited fields:
- ALLOWED: internal UUIDs, enums, dates, decimal strings, coverage terms in force,
  counts and window aggregates, product numeric inputs, hashes.
- NEVER: names, exact date of birth, addresses, contact data, government ids,
  policy or registration numbers, clinical text or codes, free text, file names or content, IP addresses.
All models configure extra = "forbid" to reject any unapproved keys at runtime.
"""

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.engine.types import (
    AssessmentComponentInput,
    AssessmentInput,
    ClaimInput,
    CoverageInput,
    DocumentInput,
    ExclusionInput,
    HistoricalClaimInput,
    PolicyInput,
    RequiredDocumentTypeInput,
    RiskInput,
    ValidationInput,
)


def compute_ruleset_hash(ruleset: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of canonical ruleset dictionary."""
    canonical_json = json.dumps(ruleset, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


# ==============================================================================
# Strict Allowlist Component Models
# ==============================================================================


class SnapshotClaimModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    customer_id: str
    policy_id: str
    claim_category: str
    incident_date: str  # ISO date
    registered_date: str  # ISO date
    requested_amount: str
    insured_item_id: str | None = None
    incident_group_id: str | None = None
    incident_location_present: bool = True
    incident_description_present: bool = True


class SnapshotPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: str
    customer_id: str
    product_type: str
    start_date: str
    end_date: str
    status: str


class SnapshotCoverageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage_id: str
    claim_category: str
    limit_amount: str
    consumed_amount: str
    deductible: str
    copay_percent: str
    waiting_period_days: int
    is_excluded: bool
    limit_exceeded_behavior: str
    limit_scope: str


class SnapshotExclusionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclusion_code: str
    description_hash: str


class SnapshotDocumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    doc_code: str
    status: str
    document_date: str | None = None
    identity_attestation_result: str | None = None
    is_identity_attestation_current: bool = False
    is_identity_bearing: bool = False


class SnapshotRequiredDocModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_code: str
    name: str
    is_mandatory: bool
    is_identity_bearing: bool = False


class SnapshotHistoricalClaimModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    customer_id: str
    policy_id: str
    claim_category: str
    incident_date: str
    registered_date: str
    requested_amount: str
    status: str
    approved_amount: str | None = None
    incident_group_id: str | None = None


class SnapshotAssessmentComponentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component_type: str
    claimed_amount: str
    assessed_amount: str
    zeroed_reason: str | None = None


# ==============================================================================
# Top-Level Run Snapshot Models
# ==============================================================================


class ValidationSnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: SnapshotClaimModel
    policy: SnapshotPolicyModel | None = None
    coverages: list[SnapshotCoverageModel]
    exclusions: list[SnapshotExclusionModel]
    documents: list[SnapshotDocumentModel]
    required_document_types: list[SnapshotRequiredDocModel]
    evaluation_date: str
    ruleset_version: str
    engine_version: str
    ruleset_hash: str | None = None


class RiskSnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: SnapshotClaimModel
    policy: SnapshotPolicyModel | None = None
    coverage: SnapshotCoverageModel | None = None
    documents: list[SnapshotDocumentModel]
    historical_claims: list[SnapshotHistoricalClaimModel]
    peer_approved_amounts: list[str]
    evaluation_date: str
    ruleset_version: str
    engine_version: str
    ruleset_hash: str | None = None
    history_coverage: dict[str, str] = Field(default_factory=dict)


class AssessmentSnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_type: str
    requested_amount: str
    remaining_limit: str
    assessed_loss: str
    deductible: str
    copay_percent: str
    depreciation: str
    sum_insured: str
    value_at_risk: str
    components: list[SnapshotAssessmentComponentModel] = Field(default_factory=list)
    ruleset_version: str = "v1"
    engine_version: str = "1.0.0"
    ruleset_hash: str | None = None


# ==============================================================================
# Serializers & Deserializers
# ==============================================================================


def serialize_validation_snapshot(
    val_input: ValidationInput, ruleset_hash: str | None = None
) -> dict[str, Any]:
    """Serialize pure ValidationInput into strict allowlist snapshot dict."""
    claim = val_input.claim
    policy = val_input.policy
    model = ValidationSnapshotModel(
        claim=SnapshotClaimModel(
            claim_id=claim.claim_id,
            customer_id=claim.customer_id,
            policy_id=claim.policy_id,
            claim_category=claim.claim_category,
            incident_date=claim.incident_date.isoformat(),
            registered_date=claim.registered_date.isoformat(),
            requested_amount=str(claim.requested_amount),
            insured_item_id=claim.insured_item_id,
            incident_group_id=claim.incident_group_id,
            incident_location_present=bool(
                claim.incident_location and claim.incident_location.strip()
            ),
            incident_description_present=bool(
                claim.incident_description and claim.incident_description.strip()
            ),
        ),
        policy=SnapshotPolicyModel(
            policy_id=policy.policy_id,
            customer_id=policy.customer_id,
            product_type=policy.product_type,
            start_date=policy.start_date.isoformat(),
            end_date=policy.end_date.isoformat(),
            status=policy.status,
        )
        if policy
        else None,
        coverages=[
            SnapshotCoverageModel(
                coverage_id=c.coverage_id,
                claim_category=c.claim_category,
                limit_amount=str(c.limit_amount),
                consumed_amount=str(c.consumed_amount),
                deductible=str(c.deductible),
                copay_percent=str(c.copay_percent),
                waiting_period_days=c.waiting_period_days,
                is_excluded=c.is_excluded,
                limit_exceeded_behavior=c.limit_exceeded_behavior,
                limit_scope=c.limit_scope,
            )
            for c in val_input.coverages
        ],
        exclusions=[
            SnapshotExclusionModel(
                exclusion_code=e.exclusion_code,
                description_hash=hashlib.sha256(e.description.encode("utf-8")).hexdigest(),
            )
            for e in val_input.exclusions
        ],
        documents=[
            SnapshotDocumentModel(
                document_id=d.document_id,
                doc_code=d.doc_code,
                status=d.status,
                document_date=d.document_date.isoformat() if d.document_date else None,
                identity_attestation_result=d.identity_attestation_result,
                is_identity_attestation_current=d.is_identity_attestation_current,
                is_identity_bearing=d.is_identity_bearing,
            )
            for d in val_input.documents
        ],
        required_document_types=[
            SnapshotRequiredDocModel(
                doc_code=r.doc_code,
                name=r.name,
                is_mandatory=r.is_mandatory,
                is_identity_bearing=r.is_identity_bearing,
            )
            for r in val_input.required_document_types
        ],
        evaluation_date=val_input.evaluation_date.isoformat(),
        ruleset_version=str(val_input.ruleset.get("ruleset_version", "v1")),
        engine_version=str(val_input.ruleset.get("engine_version", "1.0.0")),
        ruleset_hash=ruleset_hash or compute_ruleset_hash(val_input.ruleset),
    )
    return model.model_dump()


def deserialize_validation_snapshot(
    snapshot: dict[str, Any], ruleset: dict[str, Any]
) -> ValidationInput:
    """Reconstruct pure ValidationInput from stored allowlist snapshot without live DB query."""
    model = ValidationSnapshotModel.model_validate(snapshot)
    c = model.claim
    claim_input = ClaimInput(
        claim_id=c.claim_id,
        claim_ref=c.claim_id,
        customer_id=c.customer_id,
        policy_id=c.policy_id,
        claim_category=c.claim_category,
        incident_date=date.fromisoformat(c.incident_date),
        incident_location="[PRESENT]" if c.incident_location_present else "",
        incident_description="[PRESENT]" if c.incident_description_present else "",
        requested_amount=Decimal(c.requested_amount),
        registered_date=date.fromisoformat(c.registered_date),
        insured_item_id=c.insured_item_id,
        incident_group_id=c.incident_group_id,
    )

    policy_input: PolicyInput | None = None
    if model.policy:
        p = model.policy
        policy_input = PolicyInput(
            policy_id=p.policy_id,
            policy_no=p.policy_id,
            customer_id=p.customer_id,
            product_type=p.product_type,
            start_date=date.fromisoformat(p.start_date),
            end_date=date.fromisoformat(p.end_date),
            status=p.status,
        )

    coverages = tuple(
        CoverageInput(
            coverage_id=cov.coverage_id,
            claim_category=cov.claim_category,
            limit_amount=Decimal(cov.limit_amount),
            consumed_amount=Decimal(cov.consumed_amount),
            deductible=Decimal(cov.deductible),
            copay_percent=Decimal(cov.copay_percent),
            waiting_period_days=cov.waiting_period_days,
            is_excluded=cov.is_excluded,
            limit_exceeded_behavior=cov.limit_exceeded_behavior,
            limit_scope=cov.limit_scope,
        )
        for cov in model.coverages
    )

    exclusions = tuple(
        ExclusionInput(
            exclusion_code=ex.exclusion_code,
            description="[HASHED]",
        )
        for ex in model.exclusions
    )

    documents = tuple(
        DocumentInput(
            document_id=doc.document_id,
            doc_code=doc.doc_code,
            status=doc.status,
            document_date=date.fromisoformat(doc.document_date) if doc.document_date else None,
            identity_attestation_result=doc.identity_attestation_result,
            is_identity_attestation_current=doc.is_identity_attestation_current,
            is_identity_bearing=doc.is_identity_bearing,
        )
        for doc in model.documents
    )

    req_docs = tuple(
        RequiredDocumentTypeInput(
            doc_code=r.doc_code,
            name=r.name,
            is_mandatory=r.is_mandatory,
            is_identity_bearing=r.is_identity_bearing,
        )
        for r in model.required_document_types
    )

    return ValidationInput(
        claim=claim_input,
        policy=policy_input,
        coverages=coverages,
        exclusions=exclusions,
        documents=documents,
        required_document_types=req_docs,
        evaluation_date=date.fromisoformat(model.evaluation_date),
        ruleset=ruleset,
    )


def serialize_risk_snapshot(
    risk_input: RiskInput, ruleset_hash: str | None = None
) -> dict[str, Any]:
    """Serialize pure RiskInput into strict allowlist snapshot dict."""
    claim = risk_input.claim
    policy = risk_input.policy
    cov = risk_input.coverage

    model = RiskSnapshotModel(
        claim=SnapshotClaimModel(
            claim_id=claim.claim_id,
            customer_id=claim.customer_id,
            policy_id=claim.policy_id,
            claim_category=claim.claim_category,
            incident_date=claim.incident_date.isoformat(),
            registered_date=claim.registered_date.isoformat(),
            requested_amount=str(claim.requested_amount),
            insured_item_id=claim.insured_item_id,
            incident_group_id=claim.incident_group_id,
            incident_location_present=bool(
                claim.incident_location and claim.incident_location.strip()
            ),
            incident_description_present=bool(
                claim.incident_description and claim.incident_description.strip()
            ),
        ),
        policy=SnapshotPolicyModel(
            policy_id=policy.policy_id,
            customer_id=policy.customer_id,
            product_type=policy.product_type,
            start_date=policy.start_date.isoformat(),
            end_date=policy.end_date.isoformat(),
            status=policy.status,
        )
        if policy
        else None,
        coverage=SnapshotCoverageModel(
            coverage_id=cov.coverage_id,
            claim_category=cov.claim_category,
            limit_amount=str(cov.limit_amount),
            consumed_amount=str(cov.consumed_amount),
            deductible=str(cov.deductible),
            copay_percent=str(cov.copay_percent),
            waiting_period_days=cov.waiting_period_days,
            is_excluded=cov.is_excluded,
            limit_exceeded_behavior=cov.limit_exceeded_behavior,
            limit_scope=cov.limit_scope,
        )
        if cov
        else None,
        documents=[
            SnapshotDocumentModel(
                document_id=d.document_id,
                doc_code=d.doc_code,
                status=d.status,
                document_date=d.document_date.isoformat() if d.document_date else None,
                identity_attestation_result=d.identity_attestation_result,
                is_identity_attestation_current=d.is_identity_attestation_current,
                is_identity_bearing=d.is_identity_bearing,
            )
            for d in risk_input.documents
        ],
        historical_claims=[
            SnapshotHistoricalClaimModel(
                claim_id=h.claim_id,
                customer_id=h.customer_id,
                policy_id=h.policy_id,
                claim_category=h.claim_category,
                incident_date=h.incident_date.isoformat(),
                registered_date=h.registered_date.isoformat(),
                requested_amount=str(h.requested_amount),
                status=h.status,
                approved_amount=str(h.approved_amount) if h.approved_amount is not None else None,
                incident_group_id=h.incident_group_id,
            )
            for h in risk_input.historical_claims
        ],
        peer_approved_amounts=[str(a) for a in risk_input.peer_approved_amounts],
        evaluation_date=risk_input.evaluation_date.isoformat(),
        ruleset_version=str(risk_input.ruleset.get("ruleset_version", "v1")),
        engine_version=str(risk_input.ruleset.get("engine_version", "1.0.0")),
        ruleset_hash=ruleset_hash or compute_ruleset_hash(risk_input.ruleset),
        history_coverage={k: v.isoformat() for k, v in risk_input.history_coverage.items()},
    )
    return model.model_dump()


def deserialize_risk_snapshot(snapshot: dict[str, Any], ruleset: dict[str, Any]) -> RiskInput:
    """Reconstruct pure RiskInput from stored allowlist snapshot without live DB query."""
    model = RiskSnapshotModel.model_validate(snapshot)
    c = model.claim
    claim_input = ClaimInput(
        claim_id=c.claim_id,
        claim_ref=c.claim_id,
        customer_id=c.customer_id,
        policy_id=c.policy_id,
        claim_category=c.claim_category,
        incident_date=date.fromisoformat(c.incident_date),
        incident_location="[PRESENT]" if c.incident_location_present else "",
        incident_description="[PRESENT]" if c.incident_description_present else "",
        requested_amount=Decimal(c.requested_amount),
        registered_date=date.fromisoformat(c.registered_date),
        insured_item_id=c.insured_item_id,
        incident_group_id=c.incident_group_id,
    )

    policy_input: PolicyInput | None = None
    if model.policy:
        p = model.policy
        policy_input = PolicyInput(
            policy_id=p.policy_id,
            policy_no=p.policy_id,
            customer_id=p.customer_id,
            product_type=p.product_type,
            start_date=date.fromisoformat(p.start_date),
            end_date=date.fromisoformat(p.end_date),
            status=p.status,
        )

    coverage_input: CoverageInput | None = None
    if model.coverage:
        cov = model.coverage
        coverage_input = CoverageInput(
            coverage_id=cov.coverage_id,
            claim_category=cov.claim_category,
            limit_amount=Decimal(cov.limit_amount),
            consumed_amount=Decimal(cov.consumed_amount),
            deductible=Decimal(cov.deductible),
            copay_percent=Decimal(cov.copay_percent),
            waiting_period_days=cov.waiting_period_days,
            is_excluded=cov.is_excluded,
            limit_exceeded_behavior=cov.limit_exceeded_behavior,
            limit_scope=cov.limit_scope,
        )

    documents = tuple(
        DocumentInput(
            document_id=doc.document_id,
            doc_code=doc.doc_code,
            status=doc.status,
            document_date=date.fromisoformat(doc.document_date) if doc.document_date else None,
            identity_attestation_result=doc.identity_attestation_result,
            is_identity_attestation_current=doc.is_identity_attestation_current,
            is_identity_bearing=doc.is_identity_bearing,
        )
        for doc in model.documents
    )

    hist_claims = tuple(
        HistoricalClaimInput(
            claim_id=h.claim_id,
            claim_ref=h.claim_id,
            customer_id=h.customer_id,
            policy_id=h.policy_id,
            claim_category=h.claim_category,
            incident_date=date.fromisoformat(h.incident_date),
            registered_date=date.fromisoformat(h.registered_date),
            requested_amount=Decimal(h.requested_amount),
            status=h.status,
            approved_amount=Decimal(h.approved_amount) if h.approved_amount is not None else None,
            incident_group_id=h.incident_group_id,
        )
        for h in model.historical_claims
    )

    peer_amounts = tuple(Decimal(a) for a in model.peer_approved_amounts)
    hist_cov = {k: date.fromisoformat(v) for k, v in model.history_coverage.items()}

    return RiskInput(
        claim=claim_input,
        policy=policy_input,
        coverage=coverage_input,
        documents=documents,
        historical_claims=hist_claims,
        peer_approved_amounts=peer_amounts,
        evaluation_date=date.fromisoformat(model.evaluation_date),
        ruleset=ruleset,
        history_coverage=hist_cov,
    )


def serialize_assessment_snapshot(
    inp: AssessmentInput,
    ruleset_version: str = "v1",
    engine_version: str = "1.0.0",
    ruleset_hash: str | None = None,
) -> dict[str, Any]:
    """Serialize AssessmentInput into strict allowlist snapshot dict."""
    model = AssessmentSnapshotModel(
        product_type=inp.product_type,
        requested_amount=str(inp.requested_amount),
        remaining_limit=str(inp.remaining_limit),
        assessed_loss=str(inp.assessed_loss),
        deductible=str(inp.deductible),
        copay_percent=str(inp.copay_percent),
        depreciation=str(inp.depreciation),
        sum_insured=str(inp.sum_insured),
        value_at_risk=str(inp.value_at_risk),
        components=[
            SnapshotAssessmentComponentModel(
                component_type=c.component_type,
                claimed_amount=str(c.claimed_amount),
                assessed_amount=str(c.assessed_amount),
                zeroed_reason=c.zeroed_reason,
            )
            for c in inp.components
        ],
        ruleset_version=ruleset_version,
        engine_version=engine_version,
        ruleset_hash=ruleset_hash,
    )
    return model.model_dump()


def deserialize_assessment_snapshot(snapshot: dict[str, Any]) -> AssessmentInput:
    """Reconstruct pure AssessmentInput from stored allowlist snapshot without live DB query."""
    model = AssessmentSnapshotModel.model_validate(snapshot)
    components = tuple(
        AssessmentComponentInput(
            component_type=c.component_type,
            claimed_amount=Decimal(c.claimed_amount),
            assessed_amount=Decimal(c.assessed_amount),
            zeroed_reason=c.zeroed_reason,
        )
        for c in model.components
    )
    return AssessmentInput(
        product_type=model.product_type,
        requested_amount=Decimal(model.requested_amount),
        remaining_limit=Decimal(model.remaining_limit),
        assessed_loss=Decimal(model.assessed_loss),
        deductible=Decimal(model.deductible),
        copay_percent=Decimal(model.copay_percent),
        depreciation=Decimal(model.depreciation),
        sum_insured=Decimal(model.sum_insured),
        value_at_risk=Decimal(model.value_at_risk),
        components=components,
    )

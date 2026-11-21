"""Database models defining the schema for the claim processing platform."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now
from app.domain.enums import (
    AttestationResult,
    ClaimChannel,
    ClaimStatus,
    CoverageLedgerEntryType,
    DecisionType,
    DocumentStatus,
    EvidenceLevel,
    IncidentGroupStatus,
    IndicatorEvaluationStatus,
    InvestigationOutcome,
    InvestigationStatus,
    LimitScope,
    PolicyStatus,
    ProductType,
    RiskCategory,
    RiskCompleteness,
    RuleOutcome,
    RulesetLifecycle,
    UserRole,
    ValidationOutcome,
    VerificationRunType,
)

# Portable JSON type: JSONB on Postgres, JSON elsewhere
JsonType = JSON().with_variant(JSONB(), "postgresql")
BigIntType = BigInteger().with_variant(Integer, "sqlite")


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    customer: Mapped["Customer | None"] = relationship(back_populates="user")
    registered_claims: Mapped[list["Claim"]] = relationship(
        back_populates="registered_by_user", foreign_keys="Claim.registered_by"
    )


class Customer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "customers"

    customer_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dob: Mapped[date] = mapped_column(Date, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    state_region: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", default=1
    )
    source_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint("source_system", "external_ref", name="uq_customer_source_ext"),
    )

    # Relationships
    user: Mapped["User | None"] = relationship(back_populates="customer", uselist=False)
    policies: Mapped[list["Policy"]] = relationship(back_populates="customer")
    claims: Mapped[list["Claim"]] = relationship(back_populates="customer")


class Policy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "policies"

    policy_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    product_type: Mapped[ProductType] = mapped_column(
        Enum(ProductType, name="product_type"), nullable=False, index=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    premium: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    sum_insured: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[PolicyStatus] = mapped_column(
        Enum(PolicyStatus, name="policy_status"),
        nullable=False,
        default=PolicyStatus.ACTIVE,
        index=True,
    )
    source_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint("source_system", "external_ref", name="uq_policy_source_ext"),
        CheckConstraint("end_date >= start_date", name="chk_policy_date_range"),
        CheckConstraint("sum_insured >= 0", name="chk_policy_sum_insured_positive"),
        CheckConstraint("premium >= 0", name="chk_policy_premium_positive"),
    )

    # Relationships
    customer: Mapped["Customer"] = relationship(back_populates="policies")
    coverages: Mapped[list["Coverage"]] = relationship(
        back_populates="policy", cascade="all, delete-orphan"
    )
    exclusions: Mapped[list["Exclusion"]] = relationship(
        back_populates="policy", cascade="all, delete-orphan"
    )
    insured_items: Mapped[list["PolicyInsuredItem"]] = relationship(
        back_populates="policy", cascade="all, delete-orphan"
    )
    claims: Mapped[list["Claim"]] = relationship(back_populates="policy")


class Coverage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "coverages"

    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    claim_category: Mapped[str] = mapped_column(String(64), nullable=False)
    limit_scope: Mapped[LimitScope] = mapped_column(
        Enum(LimitScope, name="limit_scope"), nullable=False, default=LimitScope.PER_POLICY_PERIOD
    )
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    consumed_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    deductible: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    copay_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0.00")
    )
    waiting_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conditions: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    is_excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    limit_exceeded_behavior: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "limit_exceeded_behavior IN ('CAP', 'REJECT')",
            name="chk_coverage_limit_exceeded_behavior",
        ),
        CheckConstraint("consumed_amount >= 0", name="chk_coverage_consumed_non_negative"),
        CheckConstraint(
            "limit_scope != 'PER_POLICY_PERIOD' OR consumed_amount <= limit_amount",
            name="chk_coverage_consumed_limit",
        ),
        CheckConstraint("limit_amount >= 0", name="chk_coverage_limit_positive"),
        CheckConstraint("deductible >= 0", name="chk_coverage_deductible_positive"),
        CheckConstraint(
            "copay_percent >= 0 AND copay_percent <= 100", name="chk_coverage_copay_range"
        ),
        CheckConstraint("waiting_period_days >= 0", name="chk_coverage_waiting_period_positive"),
        Index("ix_coverage_policy_category", "policy_id", "claim_category"),
    )

    # Relationships
    policy: Mapped["Policy"] = relationship(back_populates="coverages")
    ledger_entries: Mapped[list["CoverageLedger"]] = relationship(back_populates="coverage")


class CoverageLedger(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "coverage_ledger"

    coverage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("coverages.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("decisions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    parent_ledger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("coverage_ledger.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    entry_type: Mapped[CoverageLedgerEntryType] = mapped_column(
        Enum(CoverageLedgerEntryType, name="coverage_ledger_entry_type"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_ledger_amount_positive"),
        UniqueConstraint("decision_id", "entry_type", name="uq_coverage_ledger_decision_entry"),
    )

    # Relationships
    coverage: Mapped["Coverage"] = relationship(back_populates="ledger_entries")
    claim: Mapped["Claim | None"] = relationship()
    parent_entry: Mapped["CoverageLedger | None"] = relationship(remote_side="CoverageLedger.id")
    decision: Mapped["Decision | None"] = relationship()


class Exclusion(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "exclusions"

    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    exclusion_code: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    policy: Mapped["Policy"] = relationship(back_populates="exclusions")


class PolicyInsuredItem(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "policy_insured_items"

    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_type: Mapped[str] = mapped_column(String(64), nullable=False)  # MEMBER, VEHICLE, PROPERTY
    identifier: Mapped[str] = mapped_column(String(128), nullable=False)  # Reg No / VIN / Member ID
    details: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    policy: Mapped["Policy"] = relationship(back_populates="insured_items")


class Claim(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "claims"

    claim_ref: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    insured_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policy_insured_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    incident_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    claim_category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    incident_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    incident_location: Mapped[str] = mapped_column(String(255), nullable=False)
    incident_description: Mapped[str] = mapped_column(Text, nullable=False)
    requested_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    registered_on: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    registered_tz: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    registered_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[ClaimChannel] = mapped_column(
        Enum(ClaimChannel, name="claim_channel"), nullable=False
    )
    status: Mapped[ClaimStatus] = mapped_column(
        Enum(ClaimStatus, name="claim_status"),
        nullable=False,
        default=ClaimStatus.REGISTERED,
        index=True,
    )
    status_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("requested_amount > 0", name="chk_claim_requested_amount_positive"),
        UniqueConstraint("source_system", "external_ref", name="uq_claim_source_ext"),
        UniqueConstraint("id", "customer_id", name="uq_claims_id_customer_id"),
        Index("ix_claim_status_reg_date", "status", "registered_at"),
    )

    # Relationships
    policy: Mapped["Policy"] = relationship(back_populates="claims")
    customer: Mapped["Customer"] = relationship(back_populates="claims")
    registered_by_user: Mapped["User"] = relationship(foreign_keys=[registered_by])
    insured_item: Mapped["PolicyInsuredItem | None"] = relationship()
    documents: Mapped[list["ClaimDocument"]] = relationship(back_populates="claim")
    validation_runs: Mapped[list["ValidationRun"]] = relationship(back_populates="claim")
    risk_evaluations: Mapped[list["RiskEvaluation"]] = relationship(back_populates="claim")
    assessments: Mapped[list["Assessment"]] = relationship(back_populates="claim")
    investigations: Mapped[list["Investigation"]] = relationship(back_populates="claim")
    decisions: Mapped[list["Decision"]] = relationship(back_populates="claim")
    status_history: Mapped[list["ClaimStatusHistory"]] = relationship(back_populates="claim")


class RequiredDocumentType(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "required_document_types"

    product_type: Mapped[ProductType] = mapped_column(
        Enum(ProductType, name="product_type"), nullable=False, index=True
    )
    claim_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    doc_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_identity_bearing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("product_type", "claim_category", "doc_code", name="uq_req_doc_type"),
    )


class ClaimDocument(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "claim_documents"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doc_code: Mapped[str] = mapped_column(String(64), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigIntType, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    named_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"),
        nullable=False,
        default=DocumentStatus.PENDING_REVIEW,
    )
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    # Relationships
    claim: Mapped["Claim"] = relationship(back_populates="documents")
    attestations: Mapped[list["DocumentIdentityAttestation"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentIdentityAttestation.rev",
    )

    __table_args__ = (
        UniqueConstraint("id", "sha256", name="uq_claim_document_id_sha256"),
        CheckConstraint(
            "status != 'INVALID' OR (reason_code IS NOT NULL AND review_remarks IS NOT NULL)",
            name="chk_claim_document_invalid_reason_remarks",
        ),
    )


class DocumentIdentityAttestation(Base):
    """Append-only revision table for human document identity attestations (P4 Task 5, Decision D4)."""

    __tablename__ = "document_identity_attestations"

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rev: Mapped[int] = mapped_column(Integer, nullable=False)
    prev_rev: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[AttestationResult] = mapped_column(
        Enum(AttestationResult, name="attestation_result"), nullable=False
    )
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reviewer_role: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_identity_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint("document_id", "rev", name="pk_doc_identity_attestation"),
        ForeignKeyConstraint(
            ["document_id", "prev_rev"],
            ["document_identity_attestations.document_id", "document_identity_attestations.rev"],
            name="fk_doc_attestation_prev_rev",
        ),
        ForeignKeyConstraint(
            ["document_id", "document_sha256"],
            ["claim_documents.id", "claim_documents.sha256"],
            name="fk_doc_attestation_doc_sha256",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(rev = 1 AND prev_rev IS NULL) OR (rev > 1 AND prev_rev IS NOT NULL AND prev_rev = rev - 1)",
            name="chk_doc_attestation_rev_seq",
        ),
        CheckConstraint(
            "result = 'CONFIRMED' OR (result IN ('MISMATCH', 'UNCERTAIN') AND reason_code IS NOT NULL AND remarks IS NOT NULL)",
            name="chk_doc_attestation_reason_remarks",
        ),
    )

    document: Mapped["ClaimDocument"] = relationship(
        back_populates="attestations", foreign_keys=[document_id]
    )
    reviewer: Mapped["User"] = relationship()


class ValidationRun(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "validation_runs"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ruleset_version: Mapped[str] = mapped_column(String(32), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    ruleset_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluated_on: Mapped[date] = mapped_column(Date, nullable=False)
    overall_result: Mapped[ValidationOutcome] = mapped_column(
        Enum(ValidationOutcome, name="validation_outcome"), nullable=False
    )
    inputs_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    calculation_trace: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    claim: Mapped["Claim"] = relationship(back_populates="validation_runs")
    results: Mapped[list["ValidationResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class ValidationResult(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "validation_results"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("validation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rule_id: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[RuleOutcome] = mapped_column(
        Enum(RuleOutcome, name="rule_outcome"), nullable=False
    )
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    inputs_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    run: Mapped["ValidationRun"] = relationship(back_populates="results")


class RiskEvaluation(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "risk_evaluations"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ruleset_version: Mapped[str] = mapped_column(String(32), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    ruleset_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluated_on: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[RiskCategory] = mapped_column(
        Enum(RiskCategory, name="risk_category"), nullable=False
    )
    completeness: Mapped[RiskCompleteness] = mapped_column(
        Enum(RiskCompleteness, name="risk_completeness"),
        nullable=False,
        default=RiskCompleteness.COMPLETE,
    )
    composed_label: Mapped[str] = mapped_column(
        String(64), nullable=False, default="LOW (COMPLETE)"
    )
    requires_investigation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    routing_reasons: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    pilot_banner: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="PILOT: Risk indicators are experimental and unvalidated. Manual review required.",
    )
    group_scores: Mapped[dict[str, int]] = mapped_column(JsonType, nullable=False, default=dict)
    inputs_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    calculation_trace: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "(category = 'UNASSESSED' AND score IS NULL) OR (category != 'UNASSESSED' AND score IS NOT NULL AND score >= 0 AND score <= 100)",
            name="chk_risk_score_unassessed_null",
        ),
    )

    claim: Mapped["Claim"] = relationship(back_populates="risk_evaluations")
    indicator_results: Mapped[list["RiskIndicatorResult"]] = relationship(
        back_populates="evaluation", cascade="all, delete-orphan"
    )


class RiskIndicatorResult(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "risk_indicator_results"

    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("risk_evaluations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    indicator_id: Mapped[str] = mapped_column(String(32), nullable=False)
    indicator_version: Mapped[str] = mapped_column(String(32), nullable=False)
    group: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    triggered: Mapped[bool] = mapped_column(Boolean, nullable=False)
    weight_applied: Mapped[int] = mapped_column(Integer, nullable=False)
    sufficient_alone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evidence_level: Mapped[EvidenceLevel] = mapped_column(
        Enum(EvidenceLevel, name="evidence_level"), nullable=False, default=EvidenceLevel.NONE
    )
    skip_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[IndicatorEvaluationStatus] = mapped_column(
        Enum(IndicatorEvaluationStatus, name="indicator_evaluation_status"),
        nullable=False,
        default=IndicatorEvaluationStatus.NOT_TRIGGERED,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    evaluation: Mapped["RiskEvaluation"] = relationship(back_populates="indicator_results")


class Assessment(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "assessments"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    assessor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    ruleset_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    ruleset_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assessed_loss: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    max_eligible_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    eligible_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    inputs_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    calculation_trace: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    remarks: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    __table_args__ = (
        CheckConstraint("eligible_amount <= max_eligible_amount", name="chk_assessment_cap"),
        CheckConstraint("eligible_amount >= 0", name="chk_assessment_eligible_positive"),
        CheckConstraint("assessed_loss >= 0", name="chk_assessment_loss_positive"),
    )

    claim: Mapped["Claim"] = relationship(back_populates="assessments")
    assessor: Mapped["User"] = relationship()


class Investigation(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "investigations"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    previous_investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id", ondelete="SET NULL"), nullable=True
    )
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assigned_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[InvestigationStatus] = mapped_column(
        Enum(InvestigationStatus, name="investigation_status"),
        nullable=False,
        default=InvestigationStatus.ASSIGNED,
    )
    referral_type: Mapped[str] = mapped_column(String(64), nullable=False, default="AUTOMATED")
    referral_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    indicator_feedback: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    outcome: Mapped[InvestigationOutcome | None] = mapped_column(
        Enum(InvestigationOutcome, name="investigation_outcome"), nullable=True
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    claim: Mapped["Claim"] = relationship(back_populates="investigations")
    assigned_investigator: Mapped["User | None"] = relationship(foreign_keys=[assigned_to])
    assigner: Mapped["User"] = relationship(foreign_keys=[assigned_by])
    findings: Mapped[list["InvestigationFinding"]] = relationship(back_populates="investigation")


class InvestigationFinding(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "investigation_findings"

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    finding_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    investigation: Mapped["Investigation"] = relationship(back_populates="findings")
    author: Mapped["User"] = relationship()


class Decision(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "decisions"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    decided_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[DecisionType] = mapped_column(
        Enum(DecisionType, name="decision_type"), nullable=False
    )
    approved_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    limit_applied: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    remarks: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes: Mapped[list[str] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    __table_args__ = (
        CheckConstraint("length(remarks) >= 20", name="chk_decision_remarks_min_length"),
        CheckConstraint(
            "limit_applied IS NULL OR approved_amount <= limit_applied",
            name="chk_decision_limit_applied",
        ),
    )

    claim: Mapped["Claim"] = relationship(back_populates="decisions")
    decided_by_user: Mapped["User"] = relationship()


class ClaimStatusHistory(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "claim_status_history"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    from_status: Mapped[str] = mapped_column(String(64), nullable=False)
    to_status: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    claim: Mapped["Claim"] = relationship(back_populates="status_history")
    changed_by_user: Mapped["User"] = relationship()


class AuditChainHead(Base):
    """Tracks independent hash chain heads per claim and system."""

    __tablename__ = "audit_chain_heads"

    chain_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigIntType, nullable=False, default=0)
    last_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AuditLog(Base):
    """Append-only audit log with HMAC-SHA256 hash chaining and Appendix B integrity constraints."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    chain_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(BigIntType, nullable=False)
    prev_seq: Mapped[int | None] = mapped_column(BigIntType, nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    key_id: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_table: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_pk: Mapped[str] = mapped_column(String(128), nullable=False)
    op: Mapped[str] = mapped_column(String(32), nullable=False, default="INSERT")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status_after: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_canonical: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    event_data: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("chain_key", "seq", name="uq_audit_chain_seq"),
        UniqueConstraint("chain_key", "seq", "hash", name="uq_audit_chain_seq_hash"),
        UniqueConstraint("chain_key", "prev_hash", name="uq_audit_chain_prev_hash"),
        CheckConstraint("seq >= 1", name="chk_audit_seq_positive"),
        CheckConstraint(
            "(seq = 1 AND prev_seq IS NULL) OR (seq > 1 AND prev_seq IS NOT NULL)",
            name="chk_audit_genesis_null",
        ),
        CheckConstraint(
            "prev_seq IS NULL OR prev_seq = seq - 1",
            name="chk_audit_seq_consecutive",
        ),
        ForeignKeyConstraint(
            ["chain_key", "prev_seq", "prev_hash"],
            ["audit_log.chain_key", "audit_log.seq", "audit_log.hash"],
            name="fk_audit_log_prev",
        ),
        Index("ix_audit_occurred_at", "occurred_at"),
    )


class VerificationRun(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "verification_runs"

    run_type: Mapped[VerificationRunType] = mapped_column(
        Enum(VerificationRunType, name="verification_run_type"), nullable=False
    )
    chain_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    first_broken_seq: Mapped[int | None] = mapped_column(BigIntType, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_anchor_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Ruleset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Governance lifecycle of business rulesets (P6 Task 2)."""

    __tablename__ = "rulesets"

    version: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    ruleset_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[RulesetLifecycle] = mapped_column(
        Enum(RulesetLifecycle, name="ruleset_lifecycle"),
        nullable=False,
        default=RulesetLifecycle.DRAFT,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    activated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    evidence_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backtest_report_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    risk_acceptance_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    risk_acceptance_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_yaml: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "approver_id IS NULL OR author_id != approver_id", name="chk_ruleset_four_eyes"
        ),
        CheckConstraint(
            "status NOT IN ('APPROVED', 'ACTIVE') OR (approver_id IS NOT NULL AND backtest_report_hash IS NOT NULL)",
            name="chk_ruleset_approved_reqs",
        ),
    )

    author: Mapped["User | None"] = relationship(foreign_keys=[author_id])
    approver: Mapped["User | None"] = relationship(foreign_keys=[approver_id])
    activator: Mapped["User | None"] = relationship(foreign_keys=[activated_by])


class UserSession(Base):
    __tablename__ = "user_sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    data: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    user: Mapped["User"] = relationship()


class LoginAttempt(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "login_attempts"

    identifier: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AccessLog(Base, UUIDPrimaryKeyMixin):
    """Append-only log of sensitive record and document accesses (P1 task 5)."""

    __tablename__ = "access_log"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("claim_documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    actor: Mapped["User | None"] = relationship()
    claim: Mapped["Claim"] = relationship()
    document: Mapped["ClaimDocument | None"] = relationship()


class ImportReject(Base, UUIDPrimaryKeyMixin):
    """Rejection record for master data imports with missing or invalid terms (P5 Task 5)."""

    __tablename__ = "import_rejects"

    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class HistoryCoverage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """History coverage loaded_from date per product type (P6 Task 6, Decision D14)."""

    __tablename__ = "history_coverage"

    product_type: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    loaded_from: Mapped[date] = mapped_column(Date, nullable=False)
    set_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    evidence_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

    set_by: Mapped["User | None"] = relationship()


class IncidentGroup(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Incident group linking multiple claims of the same customer (P6 Task 9, Decision D12)."""

    __tablename__ = "incident_groups"

    group_ref: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[IncidentGroupStatus] = mapped_column(
        Enum(IncidentGroupStatus, name="incident_group_status"),
        nullable=False,
        default=IncidentGroupStatus.PROPOSED,
    )
    proposed_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proposal_reason: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "customer_id", name="uq_incident_groups_id_customer_id"),
        CheckConstraint(
            "confirmed_by_id IS NULL OR proposed_by_id != confirmed_by_id",
            name="chk_incident_group_four_eyes",
        ),
    )

    customer: Mapped["Customer"] = relationship()
    proposed_by: Mapped["User"] = relationship(foreign_keys=[proposed_by_id])
    confirmed_by: Mapped["User | None"] = relationship(foreign_keys=[confirmed_by_id])
    members: Mapped[list["IncidentGroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class IncidentGroupMember(Base, UUIDPrimaryKeyMixin):
    """Append-only revision member of an incident group (P6 Task 9)."""

    __tablename__ = "incident_group_members"

    incident_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    rev: Mapped[int] = mapped_column(Integer, nullable=False)
    prev_rev: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["claim_id", "customer_id"],
            ["claims.id", "claims.customer_id"],
            name="fk_member_claim_cust",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["incident_group_id", "customer_id"],
            ["incident_groups.id", "incident_groups.customer_id"],
            name="fk_member_group_cust",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(rev = 1 AND prev_rev IS NULL) OR (rev > 1 AND prev_rev IS NOT NULL AND prev_rev = rev - 1)",
            name="chk_incident_member_rev_seq",
        ),
        UniqueConstraint(
            "incident_group_id", "claim_id", "rev", name="uq_incident_group_member_rev"
        ),
        Index(
            "uq_incident_group_member_active_claim",
            "claim_id",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    group: Mapped["IncidentGroup"] = relationship(back_populates="members")
    claim: Mapped["Claim"] = relationship(overlaps="group,members")
    created_by: Mapped["User"] = relationship()

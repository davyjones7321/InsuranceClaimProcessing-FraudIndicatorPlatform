"""Initial schema for insurance claim platform.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-19 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE IF EXISTS alembic_version ALTER COLUMN version_num TYPE VARCHAR(64);")

    # 1. customers
    op.create_table(
        "customers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_no", sa.String(64), unique=True, nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("dob", sa.Date(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("state_region", sa.String(128), nullable=False),
        sa.Column("source_system", sa.String(64), nullable=True),
        sa.Column("external_ref", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_system", "external_ref", name="uq_customer_source_ext"),
    )
    op.create_index("ix_customers_customer_no", "customers", ["customer_no"])
    op.create_index("ix_customers_email", "customers", ["email"])

    # 2. users
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "customer",
                "staff",
                "assessor",
                "investigator",
                "manager",
                "admin",
                name="user_role",
            ),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), default=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # 3. policies
    op.create_table(
        "policies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("policy_no", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "product_type",
            sa.Enum("HEALTH", "VEHICLE", "PROPERTY", name="product_type"),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("premium", sa.Numeric(14, 2), nullable=False),
        sa.Column("sum_insured", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "LAPSED", "CANCELLED", "EXPIRED", "SUSPENDED", name="policy_status"),
            nullable=False,
            default="ACTIVE",
        ),
        sa.Column("source_system", sa.String(64), nullable=True),
        sa.Column("external_ref", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("end_date >= start_date", name="chk_policy_date_range"),
        sa.CheckConstraint("sum_insured >= 0", name="chk_policy_sum_insured_positive"),
        sa.CheckConstraint("premium >= 0", name="chk_policy_premium_positive"),
        sa.UniqueConstraint("source_system", "external_ref", name="uq_policy_source_ext"),
    )
    op.create_index("ix_policies_policy_no", "policies", ["policy_no"])
    op.create_index("ix_policies_customer_id", "policies", ["customer_id"])

    # 4. coverages
    op.create_table(
        "coverages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "policy_id",
            UUID(as_uuid=True),
            sa.ForeignKey("policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("claim_category", sa.String(64), nullable=False),
        sa.Column(
            "limit_scope",
            sa.Enum("PER_CLAIM", "PER_POLICY_PERIOD", name="limit_scope"),
            nullable=False,
        ),
        sa.Column("limit_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("consumed_amount", sa.Numeric(14, 2), nullable=False, default=0.00),
        sa.Column("deductible", sa.Numeric(14, 2), nullable=False, default=0.00),
        sa.Column("copay_percent", sa.Numeric(5, 2), nullable=False, default=0.00),
        sa.Column("waiting_period_days", sa.Integer(), nullable=False, default=0),
        sa.Column("conditions", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
        sa.Column("is_excluded", sa.Boolean(), nullable=False, default=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("consumed_amount >= 0", name="chk_coverage_consumed_non_negative"),
        sa.CheckConstraint("consumed_amount <= limit_amount", name="chk_coverage_consumed_limit"),
        sa.CheckConstraint("limit_amount >= 0", name="chk_coverage_limit_positive"),
        sa.CheckConstraint("deductible >= 0", name="chk_coverage_deductible_positive"),
        sa.CheckConstraint(
            "copay_percent >= 0 AND copay_percent <= 100", name="chk_coverage_copay_range"
        ),
        sa.CheckConstraint("waiting_period_days >= 0", name="chk_coverage_waiting_period_positive"),
    )
    op.create_index("ix_coverage_policy_category", "coverages", ["policy_id", "claim_category"])

    # 5. exclusions
    op.create_table(
        "exclusions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "policy_id",
            UUID(as_uuid=True),
            sa.ForeignKey("policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("exclusion_code", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 6. policy_insured_items
    op.create_table(
        "policy_insured_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "policy_id",
            UUID(as_uuid=True),
            sa.ForeignKey("policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_type", sa.String(64), nullable=False),
        sa.Column("identifier", sa.String(128), nullable=False),
        sa.Column("details", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 7. claims
    op.create_table(
        "claims",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("claim_ref", sa.String(32), unique=True, nullable=False),
        sa.Column(
            "policy_id",
            UUID(as_uuid=True),
            sa.ForeignKey("policies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "insured_item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("policy_insured_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("claim_category", sa.String(64), nullable=False),
        sa.Column("incident_date", sa.Date(), nullable=False),
        sa.Column("incident_location", sa.String(255), nullable=False),
        sa.Column("incident_description", sa.Text(), nullable=False),
        sa.Column("requested_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "registered_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("channel", sa.Enum("CUSTOMER", "STAFF", name="claim_channel"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "REGISTERED",
                "DOCUMENTS_PENDING",
                "VALIDATION_FAILED",
                "VALIDATED",
                "RISK_EVALUATED",
                "UNDER_INVESTIGATION",
                "ESCALATED",
                "INVESTIGATION_COMPLETE",
                "READY_FOR_ASSESSMENT",
                "ASSESSED",
                "APPROVED",
                "REJECTED",
                "INFO_REQUESTED",
                "WITHDRAWN",
                name="claim_status",
            ),
            nullable=False,
            default="REGISTERED",
        ),
        sa.Column("status_version", sa.Integer(), nullable=False, default=1),
        sa.Column("source_system", sa.String(64), nullable=True),
        sa.Column("external_ref", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("requested_amount > 0", name="chk_claim_requested_amount_positive"),
        sa.UniqueConstraint("source_system", "external_ref", name="uq_claim_source_ext"),
    )
    op.create_index("ix_claims_claim_ref", "claims", ["claim_ref"])
    op.create_index("ix_claims_status_reg_date", "claims", ["status", "registered_at"])

    # 8. coverage_ledger
    op.create_table(
        "coverage_ledger",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "coverage_id",
            UUID(as_uuid=True),
            sa.ForeignKey("coverages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "claim_id",
            UUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "entry_type",
            sa.Enum("CONSUME", "REVERSE", name="coverage_ledger_entry_type"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="chk_ledger_amount_positive"),
    )

    # 9. required_document_types
    op.create_table(
        "required_document_types",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "product_type",
            sa.Enum("HEALTH", "VEHICLE", "PROPERTY", name="product_type"),
            nullable=False,
        ),
        sa.Column("claim_category", sa.String(64), nullable=True),
        sa.Column("doc_code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("is_mandatory", sa.Boolean(), nullable=False, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("product_type", "claim_category", "doc_code", name="uq_req_doc_type"),
    )

    # 10. claim_documents
    op.create_table(
        "claim_documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "claim_id",
            UUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("doc_code", sa.String(64), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.String(255), unique=True, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("named_person", sa.String(255), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PENDING_REVIEW", "VALID", "INVALID", "SUPERSEDED", name="document_status"),
            nullable=False,
            default="PENDING_REVIEW",
        ),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("review_remarks", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "uploaded_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 11. validation_runs
    op.create_table(
        "validation_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "claim_id",
            UUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ruleset_version", sa.String(32), nullable=False),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("evaluated_on", sa.Date(), nullable=False),
        sa.Column(
            "overall_result",
            sa.Enum("PASS", "FAIL", "BLOCKED_DOCS", name="validation_outcome"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 12. validation_results
    op.create_table(
        "validation_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("validation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rule_id", sa.String(32), nullable=False),
        sa.Column("rule_version", sa.String(32), nullable=False),
        sa.Column(
            "outcome", sa.Enum("PASS", "FAIL", "SKIPPED", name="rule_outcome"), nullable=False
        ),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("inputs_snapshot", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 13. risk_evaluations
    op.create_table(
        "risk_evaluations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "claim_id",
            UUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ruleset_version", sa.String(32), nullable=False),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("evaluated_on", sa.Date(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column(
            "category",
            sa.Enum("LOW", "MEDIUM", "HIGH", "UNASSESSED", name="risk_category"),
            nullable=False,
        ),
        sa.Column("requires_investigation", sa.Boolean(), nullable=False),
        sa.Column("inputs_snapshot", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("score >= 0 AND score <= 100", name="chk_risk_score_range"),
    )

    # 14. risk_indicator_results
    op.create_table(
        "risk_indicator_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "evaluation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("risk_evaluations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("indicator_id", sa.String(32), nullable=False),
        sa.Column("indicator_version", sa.String(32), nullable=False),
        sa.Column("triggered", sa.Boolean(), nullable=False),
        sa.Column("weight_applied", sa.Integer(), nullable=False),
        sa.Column("evidence", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PILOT", "VALIDATED", name="indicator_status"),
            nullable=False,
            default="PILOT",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 15. assessments
    op.create_table(
        "assessments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "claim_id",
            UUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "assessor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("assessed_loss", sa.Numeric(14, 2), nullable=False),
        sa.Column("max_eligible_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("eligible_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "calculation_trace", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("remarks", sa.Text(), nullable=False),
        sa.Column("supporting_info", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("eligible_amount <= max_eligible_amount", name="chk_assessment_cap"),
        sa.CheckConstraint("eligible_amount >= 0", name="chk_assessment_eligible_positive"),
        sa.CheckConstraint("assessed_loss >= 0", name="chk_assessment_loss_positive"),
    )

    # 16. investigations
    op.create_table(
        "investigations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "claim_id",
            UUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "previous_investigation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("investigations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assigned_to",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assigned_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "ASSIGNED", "UNDER_REVIEW", "COMPLETED", "ESCALATED", name="investigation_status"
            ),
            nullable=False,
            default="ASSIGNED",
        ),
        sa.Column(
            "outcome",
            sa.Enum("CLEARED", "CONCERNS_CONFIRMED", "INCONCLUSIVE", name="investigation_outcome"),
            nullable=True,
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 17. investigation_findings
    op.create_table(
        "investigation_findings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("investigations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("finding_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 18. decisions
    op.create_table(
        "decisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "claim_id",
            UUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "decided_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum("APPROVED", "REJECTED", "INFO_REQUESTED", name="decision_type"),
            nullable=False,
        ),
        sa.Column("approved_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=False),
        sa.Column("reason_codes", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(remarks) >= 20", name="chk_decision_remarks_min_length"),
    )

    # 19. claim_status_history
    op.create_table(
        "claim_status_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "claim_id",
            UUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(64), nullable=False),
        sa.Column("to_status", sa.String(64), nullable=False),
        sa.Column(
            "changed_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 20. audit_chain_heads
    op.create_table(
        "audit_chain_heads",
        sa.Column("chain_key", sa.String(128), primary_key=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, default=0),
        sa.Column("last_hash", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 21. audit_log
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chain_key", sa.String(128), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), nullable=True),
        sa.Column("actor_role", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("claim_id", UUID(as_uuid=True), nullable=True),
        sa.Column("event_data", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("chain_key", "seq", name="uq_audit_chain_seq"),
        sa.UniqueConstraint("chain_key", "prev_hash", name="uq_audit_chain_prev_hash"),
    )
    op.create_index("ix_audit_occurred_at", "audit_log", ["occurred_at"])

    # 22. verification_runs
    op.create_table(
        "verification_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_type", sa.Enum("INCREMENTAL", "FULL", name="verification_run_type"), nullable=False
        ),
        sa.Column("chain_key", sa.String(128), nullable=True),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("first_broken_seq", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("external_anchor_hash", sa.String(64), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 23. rulesets
    op.create_table(
        "rulesets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.String(32), unique=True, nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, default=False),
        sa.Column(
            "activated_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 24. user_sessions
    op.create_table(
        "user_sessions",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])

    # 25. login_attempts
    op.create_table(
        "login_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("identifier", sa.String(255), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_login_attempts_identifier", "login_attempts", ["identifier"])
    op.create_index("ix_login_attempts_attempted_at", "login_attempts", ["attempted_at"])


def downgrade() -> None:
    # Drop in reverse order of foreign keys
    op.drop_table("login_attempts")
    op.drop_table("user_sessions")
    op.drop_table("rulesets")
    op.drop_table("verification_runs")
    op.drop_table("audit_log")
    op.drop_table("audit_chain_heads")
    op.drop_table("claim_status_history")
    op.drop_table("decisions")
    op.drop_table("investigation_findings")
    op.drop_table("investigations")
    op.drop_table("assessments")
    op.drop_table("risk_indicator_results")
    op.drop_table("risk_evaluations")
    op.drop_table("validation_results")
    op.drop_table("validation_runs")
    op.drop_table("claim_documents")
    op.drop_table("required_document_types")
    op.drop_table("coverage_ledger")
    op.drop_table("claims")
    op.drop_table("policy_insured_items")
    op.drop_table("exclusions")
    op.drop_table("coverages")
    op.drop_table("policies")
    op.drop_table("users")
    op.drop_table("customers")

    # Drop enums
    for enum_name in (
        "verification_run_type",
        "decision_type",
        "investigation_outcome",
        "investigation_status",
        "indicator_status",
        "risk_category",
        "rule_outcome",
        "validation_outcome",
        "document_status",
        "coverage_ledger_entry_type",
        "claim_status",
        "claim_channel",
        "limit_scope",
        "policy_status",
        "product_type",
        "user_role",
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)

"""Align schema with ORM models: missing indexes, enums, unique constraints.

Revision ID: 0010_schema_alignment
Revises: 0009_claim_status_risk_data_pending
Create Date: 2026-11-23

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_schema_alignment"
down_revision = "0009_claim_status_risk_data_pending"
branch_labels = None
depends_on = None

# Enums used in column conversions
attestation_result_enum = sa.Enum("CONFIRMED", "MISMATCH", "UNCERTAIN", name="attestation_result")
incident_group_status_enum = sa.Enum(
    "PROPOSED", "CONFIRMED", "DISSOLVED", name="incident_group_status"
)
risk_completeness_enum = sa.Enum(
    "COMPLETE", "PARTIAL", "INSUFFICIENT_DATA", name="risk_completeness"
)
evidence_level_enum = sa.Enum("NONE", "REVIEWED", name="evidence_level")
indicator_evaluation_status_enum = sa.Enum(
    "TRIGGERED",
    "NOT_TRIGGERED",
    "SKIPPED",
    "NOT_APPLICABLE",
    "DISABLED",
    "PILOT",
    name="indicator_evaluation_status",
)
ruleset_lifecycle_enum = sa.Enum("DRAFT", "APPROVED", "ACTIVE", "RETIRED", name="ruleset_lifecycle")


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    # --------------------------------------------------------------------------
    # 1. 6 VARCHAR-to-enum columns
    # --------------------------------------------------------------------------
    if is_pg:
        attestation_result_enum.create(conn, checkfirst=True)
        incident_group_status_enum.create(conn, checkfirst=True)
        risk_completeness_enum.create(conn, checkfirst=True)
        evidence_level_enum.create(conn, checkfirst=True)
        indicator_evaluation_status_enum.create(conn, checkfirst=True)
        ruleset_lifecycle_enum.create(conn, checkfirst=True)

        op.execute(
            "ALTER TABLE document_identity_attestations "
            "ALTER COLUMN result TYPE attestation_result USING result::attestation_result;"
        )

        op.execute("ALTER TABLE incident_groups ALTER COLUMN status DROP DEFAULT;")
        op.execute(
            "ALTER TABLE incident_groups "
            "ALTER COLUMN status TYPE incident_group_status USING status::incident_group_status;"
        )
        op.execute(
            "ALTER TABLE incident_groups "
            "ALTER COLUMN status SET DEFAULT 'PROPOSED'::incident_group_status;"
        )

        op.execute("ALTER TABLE risk_evaluations ALTER COLUMN completeness DROP DEFAULT;")
        op.execute(
            "ALTER TABLE risk_evaluations "
            "ALTER COLUMN completeness TYPE risk_completeness USING completeness::risk_completeness;"
        )
        op.execute(
            "ALTER TABLE risk_evaluations "
            "ALTER COLUMN completeness SET DEFAULT 'COMPLETE'::risk_completeness;"
        )

        op.execute("ALTER TABLE risk_indicator_results ALTER COLUMN evidence_level DROP DEFAULT;")
        op.execute(
            "ALTER TABLE risk_indicator_results "
            "ALTER COLUMN evidence_level TYPE evidence_level USING evidence_level::evidence_level;"
        )
        op.execute(
            "ALTER TABLE risk_indicator_results "
            "ALTER COLUMN evidence_level SET DEFAULT 'NONE'::evidence_level;"
        )

        op.execute("ALTER TABLE risk_indicator_results ALTER COLUMN status DROP DEFAULT;")
        op.execute(
            "ALTER TABLE risk_indicator_results "
            "ALTER COLUMN status TYPE indicator_evaluation_status USING status::indicator_evaluation_status;"
        )
        op.execute(
            "ALTER TABLE risk_indicator_results "
            "ALTER COLUMN status SET DEFAULT 'NOT_TRIGGERED'::indicator_evaluation_status;"
        )

        op.execute("ALTER TABLE rulesets ALTER COLUMN status DROP DEFAULT;")
        op.execute(
            "ALTER TABLE rulesets "
            "ALTER COLUMN status TYPE ruleset_lifecycle USING status::ruleset_lifecycle;"
        )
        op.execute(
            "ALTER TABLE rulesets ALTER COLUMN status SET DEFAULT 'DRAFT'::ruleset_lifecycle;"
        )
    else:
        with op.batch_alter_table("document_identity_attestations") as batch_op:
            batch_op.alter_column(
                "result",
                existing_type=sa.String(32),
                type_=attestation_result_enum,
                existing_nullable=False,
            )
        with op.batch_alter_table("incident_groups") as batch_op:
            batch_op.alter_column(
                "status",
                existing_type=sa.String(32),
                type_=incident_group_status_enum,
                existing_nullable=False,
                server_default="PROPOSED",
            )
        with op.batch_alter_table("risk_evaluations") as batch_op:
            batch_op.alter_column(
                "completeness",
                existing_type=sa.String(32),
                type_=risk_completeness_enum,
                existing_nullable=False,
                server_default="COMPLETE",
            )
        with op.batch_alter_table("risk_indicator_results") as batch_op:
            batch_op.alter_column(
                "evidence_level",
                existing_type=sa.String(32),
                type_=evidence_level_enum,
                existing_nullable=False,
                server_default="NONE",
            )
            batch_op.alter_column(
                "status",
                existing_type=sa.String(32),
                type_=indicator_evaluation_status_enum,
                existing_nullable=False,
                server_default="NOT_TRIGGERED",
            )
        with op.batch_alter_table("rulesets") as batch_op:
            batch_op.alter_column(
                "status",
                existing_type=sa.String(32),
                type_=ruleset_lifecycle_enum,
                existing_nullable=False,
                server_default="DRAFT",
            )

    # --------------------------------------------------------------------------
    # 2. 5 unique-constraint differences
    # Drop inline unique constraints on PostgreSQL (uniqueness is enforced via unique indexes)
    # --------------------------------------------------------------------------
    if is_pg:
        op.drop_constraint("users_email_key", "users", type_="unique")
        op.drop_constraint("customers_customer_no_key", "customers", type_="unique")
        op.drop_constraint("policies_policy_no_key", "policies", type_="unique")
        op.drop_constraint("claims_claim_ref_key", "claims", type_="unique")
        op.drop_constraint("rulesets_version_key", "rulesets", type_="unique")

    # --------------------------------------------------------------------------
    # 3. 4 index differences (convert non-unique indexes to unique) + rename status_reg_date
    # --------------------------------------------------------------------------
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.drop_index("ix_customers_customer_no", table_name="customers")
    op.create_index("ix_customers_customer_no", "customers", ["customer_no"], unique=True)

    op.drop_index("ix_policies_policy_no", table_name="policies")
    op.create_index("ix_policies_policy_no", "policies", ["policy_no"], unique=True)

    op.drop_index("ix_claims_claim_ref", table_name="claims")
    op.create_index("ix_claims_claim_ref", "claims", ["claim_ref"], unique=True)

    op.drop_index("ix_claims_status_reg_date", table_name="claims")

    # --------------------------------------------------------------------------
    # 4. 29 missing indexes
    # --------------------------------------------------------------------------
    op.create_index("ix_assessments_claim_id", "assessments", ["claim_id"], unique=False)
    op.create_index("ix_audit_log_chain_key", "audit_log", ["chain_key"], unique=False)
    op.create_index("ix_audit_log_claim_id", "audit_log", ["claim_id"], unique=False)
    op.create_index("ix_claim_documents_claim_id", "claim_documents", ["claim_id"], unique=False)
    op.create_index(
        "ix_claim_status_history_claim_id", "claim_status_history", ["claim_id"], unique=False
    )
    op.create_index("ix_claim_status_reg_date", "claims", ["status", "registered_at"], unique=False)
    op.create_index("ix_claims_claim_category", "claims", ["claim_category"], unique=False)
    op.create_index("ix_claims_customer_id", "claims", ["customer_id"], unique=False)
    op.create_index("ix_claims_incident_date", "claims", ["incident_date"], unique=False)
    op.create_index("ix_claims_policy_id", "claims", ["policy_id"], unique=False)
    op.create_index("ix_claims_status", "claims", ["status"], unique=False)
    op.create_index("ix_coverage_ledger_claim_id", "coverage_ledger", ["claim_id"], unique=False)
    op.create_index(
        "ix_coverage_ledger_coverage_id", "coverage_ledger", ["coverage_id"], unique=False
    )
    op.create_index("ix_coverages_policy_id", "coverages", ["policy_id"], unique=False)
    op.create_index("ix_decisions_claim_id", "decisions", ["claim_id"], unique=False)
    op.create_index("ix_exclusions_policy_id", "exclusions", ["policy_id"], unique=False)
    op.create_index(
        "ix_investigation_findings_investigation_id",
        "investigation_findings",
        ["investigation_id"],
        unique=False,
    )
    op.create_index(
        "ix_investigations_assigned_to", "investigations", ["assigned_to"], unique=False
    )
    op.create_index("ix_investigations_claim_id", "investigations", ["claim_id"], unique=False)
    op.create_index("ix_policies_product_type", "policies", ["product_type"], unique=False)
    op.create_index("ix_policies_status", "policies", ["status"], unique=False)
    op.create_index(
        "ix_policy_insured_items_policy_id", "policy_insured_items", ["policy_id"], unique=False
    )
    op.create_index(
        "ix_required_document_types_product_type",
        "required_document_types",
        ["product_type"],
        unique=False,
    )
    op.create_index("ix_risk_evaluations_claim_id", "risk_evaluations", ["claim_id"], unique=False)
    op.create_index(
        "ix_risk_indicator_results_evaluation_id",
        "risk_indicator_results",
        ["evaluation_id"],
        unique=False,
    )
    op.create_index("ix_rulesets_version", "rulesets", ["version"], unique=True)
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"], unique=False)
    op.create_index("ix_validation_results_run_id", "validation_results", ["run_id"], unique=False)
    op.create_index("ix_validation_runs_claim_id", "validation_runs", ["claim_id"], unique=False)


def downgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    # 1. Drop 29 added indexes
    op.drop_index("ix_validation_runs_claim_id", table_name="validation_runs")
    op.drop_index("ix_validation_results_run_id", table_name="validation_results")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_index("ix_rulesets_version", table_name="rulesets")
    op.drop_index("ix_risk_indicator_results_evaluation_id", table_name="risk_indicator_results")
    op.drop_index("ix_risk_evaluations_claim_id", table_name="risk_evaluations")
    op.drop_index("ix_required_document_types_product_type", table_name="required_document_types")
    op.drop_index("ix_policy_insured_items_policy_id", table_name="policy_insured_items")
    op.drop_index("ix_policies_status", table_name="policies")
    op.drop_index("ix_policies_product_type", table_name="policies")
    op.drop_index("ix_investigations_claim_id", table_name="investigations")
    op.drop_index("ix_investigations_assigned_to", table_name="investigations")
    op.drop_index("ix_investigation_findings_investigation_id", table_name="investigation_findings")
    op.drop_index("ix_exclusions_policy_id", table_name="exclusions")
    op.drop_index("ix_decisions_claim_id", table_name="decisions")
    op.drop_index("ix_coverages_policy_id", table_name="coverages")
    op.drop_index("ix_coverage_ledger_coverage_id", table_name="coverage_ledger")
    op.drop_index("ix_coverage_ledger_claim_id", table_name="coverage_ledger")
    op.drop_index("ix_claims_status", table_name="claims")
    op.drop_index("ix_claims_policy_id", table_name="claims")
    op.drop_index("ix_claims_incident_date", table_name="claims")
    op.drop_index("ix_claims_customer_id", table_name="claims")
    op.drop_index("ix_claims_claim_category", table_name="claims")
    op.drop_index("ix_claim_status_reg_date", table_name="claims")
    op.drop_index("ix_claim_status_history_claim_id", table_name="claim_status_history")
    op.drop_index("ix_claim_documents_claim_id", table_name="claim_documents")
    op.drop_index("ix_audit_log_claim_id", table_name="audit_log")
    op.drop_index("ix_audit_log_chain_key", table_name="audit_log")
    op.drop_index("ix_assessments_claim_id", table_name="assessments")

    # 2. Revert 4 unique indexes to non-unique and re-add status_reg_date
    op.create_index(
        "ix_claims_status_reg_date", "claims", ["status", "registered_at"], unique=False
    )

    op.drop_index("ix_claims_claim_ref", table_name="claims")
    op.create_index("ix_claims_claim_ref", "claims", ["claim_ref"], unique=False)

    op.drop_index("ix_policies_policy_no", table_name="policies")
    op.create_index("ix_policies_policy_no", "policies", ["policy_no"], unique=False)

    op.drop_index("ix_customers_customer_no", table_name="customers")
    op.create_index("ix_customers_customer_no", "customers", ["customer_no"], unique=False)

    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=False)

    # 3. Recreate 5 unique constraints on PostgreSQL
    if is_pg:
        op.create_unique_constraint("rulesets_version_key", "rulesets", ["version"])
        op.create_unique_constraint("claims_claim_ref_key", "claims", ["claim_ref"])
        op.create_unique_constraint("policies_policy_no_key", "policies", ["policy_no"])
        op.create_unique_constraint("customers_customer_no_key", "customers", ["customer_no"])
        op.create_unique_constraint("users_email_key", "users", ["email"])

    # 4. Revert 6 columns to VARCHAR(32)
    if is_pg:
        op.execute("ALTER TABLE rulesets ALTER COLUMN status DROP DEFAULT;")
        op.execute("ALTER TABLE rulesets ALTER COLUMN status TYPE VARCHAR(32) USING status::text;")
        op.execute("ALTER TABLE rulesets ALTER COLUMN status SET DEFAULT 'DRAFT';")

        op.execute("ALTER TABLE risk_indicator_results ALTER COLUMN status DROP DEFAULT;")
        op.execute(
            "ALTER TABLE risk_indicator_results ALTER COLUMN status TYPE VARCHAR(32) USING status::text;"
        )
        op.execute(
            "ALTER TABLE risk_indicator_results ALTER COLUMN status SET DEFAULT 'TRIGGERED';"
        )

        op.execute("ALTER TABLE risk_indicator_results ALTER COLUMN evidence_level DROP DEFAULT;")
        op.execute(
            "ALTER TABLE risk_indicator_results ALTER COLUMN evidence_level TYPE VARCHAR(32) USING evidence_level::text;"
        )
        op.execute(
            "ALTER TABLE risk_indicator_results ALTER COLUMN evidence_level SET DEFAULT 'NONE';"
        )

        op.execute("ALTER TABLE risk_evaluations ALTER COLUMN completeness DROP DEFAULT;")
        op.execute(
            "ALTER TABLE risk_evaluations ALTER COLUMN completeness TYPE VARCHAR(32) USING completeness::text;"
        )
        op.execute("ALTER TABLE risk_evaluations ALTER COLUMN completeness SET DEFAULT 'COMPLETE';")

        op.execute("ALTER TABLE incident_groups ALTER COLUMN status DROP DEFAULT;")
        op.execute(
            "ALTER TABLE incident_groups ALTER COLUMN status TYPE VARCHAR(32) USING status::text;"
        )
        op.execute("ALTER TABLE incident_groups ALTER COLUMN status SET DEFAULT 'PROPOSED';")

        op.execute(
            "ALTER TABLE document_identity_attestations "
            "ALTER COLUMN result TYPE VARCHAR(32) USING result::text;"
        )

        ruleset_lifecycle_enum.drop(conn, checkfirst=True)
        indicator_evaluation_status_enum.drop(conn, checkfirst=True)
        evidence_level_enum.drop(conn, checkfirst=True)
        risk_completeness_enum.drop(conn, checkfirst=True)
        incident_group_status_enum.drop(conn, checkfirst=True)
        attestation_result_enum.drop(conn, checkfirst=True)
    else:
        with op.batch_alter_table("rulesets") as batch_op:
            batch_op.alter_column(
                "status",
                existing_type=ruleset_lifecycle_enum,
                type_=sa.String(32),
                existing_nullable=False,
                server_default="DRAFT",
            )
        with op.batch_alter_table("risk_indicator_results") as batch_op:
            batch_op.alter_column(
                "status",
                existing_type=indicator_evaluation_status_enum,
                type_=sa.String(32),
                existing_nullable=False,
                server_default="TRIGGERED",
            )
            batch_op.alter_column(
                "evidence_level",
                existing_type=evidence_level_enum,
                type_=sa.String(32),
                existing_nullable=False,
                server_default="NONE",
            )
        with op.batch_alter_table("risk_evaluations") as batch_op:
            batch_op.alter_column(
                "completeness",
                existing_type=risk_completeness_enum,
                type_=sa.String(32),
                existing_nullable=False,
                server_default="COMPLETE",
            )
        with op.batch_alter_table("incident_groups") as batch_op:
            batch_op.alter_column(
                "status",
                existing_type=incident_group_status_enum,
                type_=sa.String(32),
                existing_nullable=False,
                server_default="PROPOSED",
            )
        with op.batch_alter_table("document_identity_attestations") as batch_op:
            batch_op.alter_column(
                "result",
                existing_type=attestation_result_enum,
                type_=sa.String(32),
                existing_nullable=False,
            )

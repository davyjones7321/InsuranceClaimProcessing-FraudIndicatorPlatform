"""Risk governance, incident groups, ruleset lifecycle, and history coverage (Phase P6).

Revision ID: 0007_risk_governance
Revises: 0006_assessment_engine_replay_and_snapshots
Create Date: 2026-11-21 11:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0007_risk_governance"
down_revision: str | None = "0006_assessment_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    uuid_type = UUID(as_uuid=True) if is_pg else sa.String(36)
    json_type = JSONB if is_pg else sa.JSON()

    if is_pg:
        op.execute("ALTER TYPE risk_category ADD VALUE IF NOT EXISTS 'UNASSESSED';")
        op.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'indicator_status') THEN
                    ALTER TYPE indicator_status ADD VALUE IF NOT EXISTS 'PILOT';
                END IF;
            END $$;
            """
        )

    # 1. Create history_coverage table
    op.create_table(
        "history_coverage",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("product_type", sa.String(32), unique=True, nullable=False, index=True),
        sa.Column("loaded_from", sa.Date(), nullable=False),
        sa.Column(
            "set_by_id", uuid_type, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column("evidence_reference", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # 2. Update existing rulesets table with lifecycle & four-eyes approval
    with op.batch_alter_table("rulesets") as batch_op:
        batch_op.add_column(
            sa.Column("engine_version", sa.String(32), nullable=False, server_default="1.0.0")
        )
        batch_op.add_column(
            sa.Column("ruleset_hash", sa.String(64), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("status", sa.String(32), nullable=False, server_default="DRAFT")
        )
        batch_op.add_column(sa.Column("author_id", uuid_type, nullable=True))
        batch_op.add_column(sa.Column("approver_id", uuid_type, nullable=True))
        batch_op.create_foreign_key(
            "fk_ruleset_author", "users", ["author_id"], ["id"], ondelete="RESTRICT"
        )
        batch_op.create_foreign_key(
            "fk_ruleset_approver", "users", ["approver_id"], ["id"], ondelete="RESTRICT"
        )
        batch_op.add_column(sa.Column("evidence_reference", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("backtest_report_hash", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("review_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("risk_acceptance_by", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("risk_acceptance_remarks", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("content_yaml", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch_op.create_check_constraint(
            "chk_ruleset_four_eyes", "approver_id IS NULL OR author_id != approver_id"
        )
        batch_op.create_check_constraint(
            "chk_ruleset_approved_reqs",
            "status NOT IN ('APPROVED', 'ACTIVE') OR (approver_id IS NOT NULL AND backtest_report_hash IS NOT NULL)",
        )

    # 3. Add composite unique constraint to claims (id, customer_id) for foreign key verification
    with op.batch_alter_table("claims") as batch_op:
        batch_op.create_unique_constraint("uq_claims_id_customer_id", ["id", "customer_id"])

    # 4. Create incident_groups table
    op.create_table(
        "incident_groups",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("group_ref", sa.String(32), unique=True, nullable=False, index=True),
        sa.Column(
            "customer_id",
            uuid_type,
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="PROPOSED"),
        sa.Column(
            "proposed_by_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "confirmed_by_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proposal_reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("id", "customer_id", name="uq_incident_groups_id_customer_id"),
        sa.CheckConstraint(
            "confirmed_by_id IS NULL OR proposed_by_id != confirmed_by_id",
            name="chk_incident_group_four_eyes",
        ),
    )

    # 5. Create incident_group_members table
    op.create_table(
        "incident_group_members",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("incident_group_id", uuid_type, nullable=False, index=True),
        sa.Column("claim_id", uuid_type, nullable=False, index=True),
        sa.Column("customer_id", uuid_type, nullable=False, index=True),
        sa.Column("rev", sa.Integer(), nullable=False),
        sa.Column("prev_rev", sa.Integer(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true" if is_pg else "1"),
        ),
        sa.Column(
            "created_by_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["claim_id", "customer_id"],
            ["claims.id", "claims.customer_id"],
            name="fk_member_claim_cust",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["incident_group_id", "customer_id"],
            ["incident_groups.id", "incident_groups.customer_id"],
            name="fk_member_group_cust",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(rev = 1 AND prev_rev IS NULL) OR (rev > 1 AND prev_rev IS NOT NULL AND prev_rev = rev - 1)",
            name="chk_incident_member_rev_seq",
        ),
        sa.UniqueConstraint(
            "incident_group_id", "claim_id", "rev", name="uq_incident_group_member_rev"
        ),
    )

    # Create partial unique index: only 1 active group membership per claim
    if is_pg:
        op.create_index(
            "uq_incident_group_member_active_claim",
            "incident_group_members",
            ["claim_id"],
            unique=True,
            postgresql_where=sa.text("is_active = true"),
        )
    else:
        op.create_index(
            "uq_incident_group_member_active_claim",
            "incident_group_members",
            ["claim_id"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
        )

    # 6. Update risk_evaluations table
    with op.batch_alter_table("risk_evaluations") as batch_op:
        batch_op.alter_column("score", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(
            sa.Column(
                "completeness",
                sa.String(32),
                nullable=False,
                server_default="COMPLETE",
            )
        )
        batch_op.add_column(
            sa.Column(
                "composed_label",
                sa.String(64),
                nullable=False,
                server_default="LOW (COMPLETE)",
            )
        )
        batch_op.add_column(
            sa.Column(
                "routing_reasons",
                json_type,
                nullable=False,
                server_default="[]" if not is_pg else sa.text("'[]'::jsonb"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "pilot_banner",
                sa.Text(),
                nullable=False,
                server_default="PILOT: Risk indicators are experimental and unvalidated. Manual review required.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "group_scores",
                json_type,
                nullable=False,
                server_default="{}" if not is_pg else sa.text("'{}'::jsonb"),
            )
        )
        # Drop old score range check and add unassessed check
        try:
            batch_op.drop_constraint("chk_risk_score_range", type_="check")
        except Exception:
            pass
        batch_op.create_check_constraint(
            "chk_risk_score_unassessed_null",
            "(category = 'UNASSESSED' AND score IS NULL) OR (category != 'UNASSESSED' AND score IS NOT NULL AND score >= 0 AND score <= 100)",
        )

    # 7. Update risk_indicator_results table
    with op.batch_alter_table("risk_indicator_results") as batch_op:
        batch_op.add_column(sa.Column("group", sa.String(32), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("weight", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(
            sa.Column(
                "sufficient_alone",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false" if is_pg else "0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "evidence_level",
                sa.String(32),
                nullable=False,
                server_default="NONE",
            )
        )
        batch_op.add_column(sa.Column("skip_reason", sa.String(255), nullable=True))
        batch_op.alter_column(
            "status",
            existing_type=sa.String(32),
            type_=sa.String(32),
            nullable=False,
            server_default="NOT_TRIGGERED",
            postgresql_using="status::text",
        )

    # 8. Update investigations table: referral type, referral remarks, indicator feedback
    with op.batch_alter_table("investigations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "referral_type",
                sa.String(64),
                nullable=False,
                server_default="AUTOMATED",
            )
        )
        batch_op.add_column(sa.Column("referral_remarks", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("indicator_feedback", json_type, nullable=True))


def downgrade() -> None:
    # Drop columns on investigations
    with op.batch_alter_table("investigations") as batch_op:
        batch_op.drop_column("indicator_feedback")
        batch_op.drop_column("referral_remarks")
        batch_op.drop_column("referral_type")

    # Drop columns on risk_indicator_results
    with op.batch_alter_table("risk_indicator_results") as batch_op:
        batch_op.drop_column("skip_reason")
        batch_op.drop_column("evidence_level")
        batch_op.drop_column("sufficient_alone")
        batch_op.drop_column("weight")
        batch_op.drop_column("group")

    # Drop columns on risk_evaluations
    with op.batch_alter_table("risk_evaluations") as batch_op:
        batch_op.drop_constraint("chk_risk_score_unassessed_null", type_="check")
        batch_op.drop_column("group_scores")
        batch_op.drop_column("pilot_banner")
        batch_op.drop_column("routing_reasons")
        batch_op.drop_column("composed_label")
        batch_op.drop_column("completeness")
        batch_op.alter_column("score", existing_type=sa.Integer(), nullable=False)
        batch_op.create_check_constraint("chk_risk_score_range", "score >= 0 AND score <= 100")

    # Drop incident_group_members
    op.drop_index("uq_incident_group_member_active_claim", table_name="incident_group_members")
    op.drop_table("incident_group_members")

    # Drop incident_groups
    op.drop_table("incident_groups")

    # Drop unique constraint on claims
    with op.batch_alter_table("claims") as batch_op:
        batch_op.drop_constraint("uq_claims_id_customer_id", type_="unique")

    # Drop columns on rulesets
    with op.batch_alter_table("rulesets") as batch_op:
        batch_op.drop_constraint("chk_ruleset_approved_reqs", type_="check")
        batch_op.drop_constraint("chk_ruleset_four_eyes", type_="check")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("retired_at")
        batch_op.drop_column("content_yaml")
        batch_op.drop_column("risk_acceptance_remarks")
        batch_op.drop_column("risk_acceptance_by")
        batch_op.drop_column("review_date")
        batch_op.drop_column("backtest_report_hash")
        batch_op.drop_column("evidence_reference")
        batch_op.drop_column("approver_id")
        batch_op.drop_column("author_id")
        batch_op.drop_column("status")
        batch_op.drop_column("ruleset_hash")
        batch_op.drop_column("engine_version")

    # Drop history_coverage
    op.drop_table("history_coverage")

"""Coverage transactional accounting, limit behavior, and audit integrity constraints.

Revision ID: 0003_coverage_transactional_accounting
Revises: 0002_access_log
Create Date: 2026-11-20 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0003_coverage_accounting"
down_revision: str | None = "0002_access_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Update coverages table
    with op.batch_alter_table("coverages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "limit_exceeded_behavior",
                sa.String(32),
                nullable=False,
                server_default="CAP",
            )
        )
        batch_op.create_check_constraint(
            "chk_coverage_limit_exceeded_behavior",
            "limit_exceeded_behavior IN ('CAP', 'REJECT')",
        )

    # 2. Update decisions table
    with op.batch_alter_table("decisions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "limit_applied",
                sa.Numeric(14, 2),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "chk_decision_limit_applied",
            "limit_applied IS NULL OR approved_amount <= limit_applied",
        )

    # 3. Update coverage_ledger table
    with op.batch_alter_table("coverage_ledger") as batch_op:
        batch_op.add_column(
            sa.Column(
                "decision_id",
                UUID(as_uuid=True),
                sa.ForeignKey(
                    "decisions.id", ondelete="RESTRICT", name="fk_coverage_ledger_decision_id"
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "parent_ledger_id",
                UUID(as_uuid=True),
                sa.ForeignKey(
                    "coverage_ledger.id",
                    ondelete="RESTRICT",
                    name="fk_coverage_ledger_parent_ledger_id",
                ),
                nullable=True,
            )
        )
        batch_op.create_index("ix_coverage_ledger_decision_id", ["decision_id"])
        batch_op.create_index("ix_coverage_ledger_parent_ledger_id", ["parent_ledger_id"])
        batch_op.create_unique_constraint(
            "uq_coverage_ledger_decision_entry",
            ["decision_id", "entry_type"],
        )

    # 4. Update assessments table
    with op.batch_alter_table("assessments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_superseded",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("assessments") as batch_op:
        batch_op.drop_column("is_superseded")

    with op.batch_alter_table("coverage_ledger") as batch_op:
        batch_op.drop_constraint("fk_coverage_ledger_parent_ledger_id", type_="foreignkey")
        batch_op.drop_constraint("fk_coverage_ledger_decision_id", type_="foreignkey")
        batch_op.drop_constraint("uq_coverage_ledger_decision_entry", type_="unique")
        batch_op.drop_index("ix_coverage_ledger_parent_ledger_id")
        batch_op.drop_index("ix_coverage_ledger_decision_id")
        batch_op.drop_column("parent_ledger_id")
        batch_op.drop_column("decision_id")

    with op.batch_alter_table("decisions") as batch_op:
        batch_op.drop_constraint("chk_decision_limit_applied", type_="check")
        batch_op.drop_column("limit_applied")

    with op.batch_alter_table("coverages") as batch_op:
        batch_op.drop_constraint("chk_coverage_limit_exceeded_behavior", type_="check")
        batch_op.drop_column("limit_exceeded_behavior")

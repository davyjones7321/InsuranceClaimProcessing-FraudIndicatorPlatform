"""Operations job_runs, legal_hold retention flag, and operational tooling (Phase P7).

Revision ID: 0008_operations_job_runs_and_retention
Revises: 0007_risk_governance
Create Date: 2026-11-21 15:53:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from alembic import op

revision: str = "0008_operations_job_runs_and_retention"
down_revision: str | None = "0007_risk_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_pg else sqlite.BLOB
    json_type = postgresql.JSONB if is_pg else sqlite.JSON

    # 1. Add legal_hold column to claims
    with op.batch_alter_table("claims") as batch_op:
        batch_op.add_column(
            sa.Column(
                "legal_hold",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false" if is_pg else "0"),
            )
        )
        batch_op.create_index("ix_claims_legal_hold", ["legal_hold"])

    # 2. Create job_runs table
    op.create_table(
        "job_runs",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("job_name", sa.String(64), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False, index=True, server_default="RUNNING"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("records_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details", json_type, nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # 1. Drop job_runs table
    op.drop_table("job_runs")

    # 2. Drop legal_hold from claims
    with op.batch_alter_table("claims") as batch_op:
        batch_op.drop_index("ix_claims_legal_hold")
        batch_op.drop_column("legal_hold")

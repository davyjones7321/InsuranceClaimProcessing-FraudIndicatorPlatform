"""Add RISK_DATA_PENDING to claim_status enum.

Revision ID: 0009_claim_status_risk_data_pending
Revises: 0008_operations_jobs
Create Date: 2026-11-23

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_claim_status_risk_data_pending"
down_revision = "0008_operations_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    is_pg = op.get_bind().dialect.name == "postgresql"
    if is_pg:
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE claim_status ADD VALUE IF NOT EXISTS 'RISK_DATA_PENDING' AFTER 'VALIDATED';"
            )


def downgrade() -> None:
    # Downgrade limits:
    # PostgreSQL does not support dropping enum values via ALTER TYPE ... DROP VALUE.
    # Reverting an enum value addition in PostgreSQL requires creating a replacement enum type,
    # converting all referring table columns, and dropping the old enum type.
    # If any claims have transitioned to RISK_DATA_PENDING, in-place removal is destructive
    # and not supported automatically.
    # In SQLite, enums are represented as VARCHAR/TEXT so no schema alteration is needed.
    pass

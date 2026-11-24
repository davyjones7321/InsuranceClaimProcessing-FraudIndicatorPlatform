"""Assessment engine snapshots, replay support, claim timezone, and import rejects (Phase P5).

Revision ID: 0006_assessment_engine_replay_and_snapshots
Revises: 0005_document_lifecycle_and_identity_attestation
Create Date: 2026-11-20 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0006_assessment_engine"
down_revision: str | None = "0005_document_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    uuid_type = UUID(as_uuid=True) if is_pg else sa.String(36)
    json_type = JSONB if is_pg else sa.JSON()

    # 1. Update claims table: incident_group_id, registered_on, registered_tz
    with op.batch_alter_table("claims") as batch_op:
        batch_op.add_column(sa.Column("incident_group_id", uuid_type, nullable=True))
        batch_op.add_column(
            sa.Column(
                "registered_on",
                sa.Date(),
                nullable=False,
                server_default=sa.text("CURRENT_DATE"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "registered_tz",
                sa.String(64),
                nullable=False,
                server_default="Asia/Kolkata",
            )
        )
        batch_op.create_index("ix_claims_incident_group_id", ["incident_group_id"])

    # 2. Update assessments table: ruleset_version, engine_version, ruleset_hash, inputs_snapshot
    with op.batch_alter_table("assessments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "ruleset_version",
                sa.String(32),
                nullable=False,
                server_default="v1",
            )
        )
        batch_op.add_column(
            sa.Column(
                "engine_version",
                sa.String(32),
                nullable=False,
                server_default="1.0.0",
            )
        )
        batch_op.add_column(sa.Column("ruleset_hash", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "inputs_snapshot",
                json_type,
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )

    # 3. Update validation_runs table: ruleset_hash, inputs_snapshot, calculation_trace
    with op.batch_alter_table("validation_runs") as batch_op:
        batch_op.add_column(sa.Column("ruleset_hash", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "inputs_snapshot",
                json_type,
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch_op.add_column(sa.Column("calculation_trace", json_type, nullable=True))

    # 4. Update risk_evaluations table: ruleset_hash, calculation_trace
    with op.batch_alter_table("risk_evaluations") as batch_op:
        batch_op.add_column(sa.Column("ruleset_hash", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("calculation_trace", json_type, nullable=True))

    # 5. Create import_rejects table
    op.create_table(
        "import_rejects",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("batch_id", uuid_type, nullable=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("external_ref", sa.String(128), nullable=True),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_import_rejects_batch_id", "import_rejects", ["batch_id"])
    op.create_index("ix_import_rejects_entity_type", "import_rejects", ["entity_type"])


def downgrade() -> None:
    op.drop_index("ix_import_rejects_entity_type", table_name="import_rejects")
    op.drop_index("ix_import_rejects_batch_id", table_name="import_rejects")
    op.drop_table("import_rejects")

    with op.batch_alter_table("risk_evaluations") as batch_op:
        batch_op.drop_column("calculation_trace")
        batch_op.drop_column("ruleset_hash")

    with op.batch_alter_table("validation_runs") as batch_op:
        batch_op.drop_column("calculation_trace")
        batch_op.drop_column("inputs_snapshot")
        batch_op.drop_column("ruleset_hash")

    with op.batch_alter_table("assessments") as batch_op:
        batch_op.drop_column("inputs_snapshot")
        batch_op.drop_column("ruleset_hash")
        batch_op.drop_column("engine_version")
        batch_op.drop_column("ruleset_version")

    with op.batch_alter_table("claims") as batch_op:
        batch_op.drop_index("ix_claims_incident_group_id")
        batch_op.drop_column("registered_tz")
        batch_op.drop_column("registered_on")
        batch_op.drop_column("incident_group_id")

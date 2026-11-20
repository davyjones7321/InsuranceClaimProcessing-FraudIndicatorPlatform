"""Audit integrity append-only triggers, HMAC constraints, and access log hashing.

Revision ID: 0004_audit_integrity_append_only
Revises: 0003_coverage_transactional_accounting
Create Date: 2026-11-20 15:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0004_audit_integrity_append_only"
down_revision: str | None = "0003_coverage_transactional_accounting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED_TABLES = [
    "audit_log",
    "claim_status_history",
    "decisions",
    "investigation_findings",
    "coverage_ledger",
    "access_log",
]


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # 1. Update audit_log schema
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.add_column(sa.Column("prev_seq", sa.BigInteger(), nullable=True))
        batch_op.add_column(
            sa.Column("hash_version", sa.SmallInteger(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("key_id", sa.SmallInteger(), nullable=False, server_default="1")
        )
        if is_pg:
            batch_op.add_column(
                sa.Column(
                    "event_id",
                    UUID(as_uuid=True),
                    nullable=False,
                    server_default=sa.text("gen_random_uuid()"),
                )
            )
        else:
            batch_op.add_column(
                sa.Column(
                    "event_id",
                    sa.String(36),
                    nullable=False,
                    server_default="00000000-0000-0000-0000-000000000000",
                )
            )
        batch_op.add_column(
            sa.Column("entity_table", sa.String(64), nullable=False, server_default="claims")
        )
        batch_op.add_column(
            sa.Column("entity_pk", sa.String(128), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("op", sa.String(32), nullable=False, server_default="INSERT"))
        batch_op.add_column(sa.Column("status_after", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("payload_canonical", sa.Text(), nullable=False, server_default="{}")
        )

        batch_op.create_unique_constraint("uq_audit_chain_seq", ["chain_key", "seq"])
        batch_op.create_unique_constraint("uq_audit_chain_seq_hash", ["chain_key", "seq", "hash"])
        batch_op.create_unique_constraint("uq_audit_chain_prev_hash", ["chain_key", "prev_hash"])
        batch_op.create_check_constraint("chk_audit_seq_positive", "seq >= 1")
        batch_op.create_check_constraint(
            "chk_audit_genesis_null",
            "(seq = 1 AND prev_seq IS NULL) OR (seq > 1 AND prev_seq IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "chk_audit_seq_consecutive",
            "prev_seq IS NULL OR prev_seq = seq - 1",
        )
        batch_op.create_foreign_key(
            "fk_audit_log_prev",
            "audit_log",
            ["chain_key", "prev_seq", "prev_hash"],
            ["chain_key", "seq", "hash"],
        )

    # 2. Update access_log schema
    with op.batch_alter_table("access_log") as batch_op:
        batch_op.add_column(sa.Column("row_hash", sa.String(64), nullable=False, server_default=""))

    # 3. Add defense-in-depth append-only triggers
    if is_pg:
        op.execute(
            """
            CREATE OR REPLACE FUNCTION trg_prevent_update_delete_truncate()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'Table % is strictly append-only: UPDATE and DELETE are prohibited', TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        for tbl in PROTECTED_TABLES:
            op.execute(
                f"""
                CREATE OR REPLACE TRIGGER trg_no_update_delete_{tbl}
                BEFORE UPDATE OR DELETE ON {tbl}
                FOR EACH ROW EXECUTE FUNCTION trg_prevent_update_delete_truncate();
                """
            )
            # Revoke permissions if app_rw role exists
            op.execute(
                f"""
                DO $$
                BEGIN
                    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_rw') THEN
                        REVOKE UPDATE, DELETE, TRUNCATE ON {tbl} FROM app_rw;
                    END IF;
                END $$;
                """
            )
    else:
        # SQLite triggers
        for tbl in PROTECTED_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_no_update_{tbl}
                BEFORE UPDATE ON {tbl}
                BEGIN
                    SELECT RAISE(ABORT, 'Table {tbl} is append-only: UPDATE is prohibited');
                END;
                """
            )
            op.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_no_delete_{tbl}
                BEFORE DELETE ON {tbl}
                BEGIN
                    SELECT RAISE(ABORT, 'Table {tbl} is append-only: DELETE is prohibited');
                END;
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # Drop triggers
    if is_pg:
        for tbl in PROTECTED_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_no_update_delete_{tbl} ON {tbl};")
        op.execute("DROP FUNCTION IF EXISTS trg_prevent_update_delete_truncate();")
    else:
        for tbl in PROTECTED_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_no_update_{tbl};")
            op.execute(f"DROP TRIGGER IF EXISTS trg_no_delete_{tbl};")

    with op.batch_alter_table("access_log") as batch_op:
        batch_op.drop_column("row_hash")

    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.drop_constraint("fk_audit_log_prev", type_="foreignkey")
        batch_op.drop_constraint("chk_audit_seq_consecutive", type_="check")
        batch_op.drop_constraint("chk_audit_genesis_null", type_="check")
        batch_op.drop_constraint("chk_audit_seq_positive", type_="check")
        batch_op.drop_constraint("uq_audit_chain_prev_hash", type_="unique")
        batch_op.drop_constraint("uq_audit_chain_seq_hash", type_="unique")
        batch_op.drop_constraint("uq_audit_chain_seq", type_="unique")

        batch_op.drop_column("payload_canonical")
        batch_op.drop_column("status_after")
        batch_op.drop_column("op")
        batch_op.drop_column("entity_pk")
        batch_op.drop_column("entity_table")
        batch_op.drop_column("event_id")
        batch_op.drop_column("key_id")
        batch_op.drop_column("hash_version")
        batch_op.drop_column("prev_seq")

"""Document lifecycle, identity attestation revision table, and document immutability triggers (Phase P4).

Revision ID: 0005_document_lifecycle_and_identity_attestation
Revises: 0004_audit_integrity_append_only
Create Date: 2026-11-20 18:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0005_document_lifecycle_and_identity_attestation"
down_revision: str | None = "0004_audit_integrity_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # 1. Add identity_version to customers
    with op.batch_alter_table("customers") as batch_op:
        batch_op.add_column(
            sa.Column("identity_version", sa.Integer(), nullable=False, server_default="1")
        )

    # 2. Add is_identity_bearing to required_document_types
    with op.batch_alter_table("required_document_types") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_identity_bearing", sa.Boolean(), nullable=False, server_default=sa.text("false")
            )
        )

    # 3. Add constraints to claim_documents
    with op.batch_alter_table("claim_documents") as batch_op:
        batch_op.create_unique_constraint("uq_claim_document_id_sha256", ["id", "sha256"])
        batch_op.create_check_constraint(
            "chk_claim_document_invalid_reason_remarks",
            "status != 'INVALID' OR (reason_code IS NOT NULL AND review_remarks IS NOT NULL)",
        )

    # 4. Create document_identity_attestations revision table
    uuid_type = UUID(as_uuid=True) if is_pg else sa.String(36)
    op.create_table(
        "document_identity_attestations",
        sa.Column("document_id", uuid_type, nullable=False),
        sa.Column("rev", sa.Integer(), nullable=False),
        sa.Column("prev_rev", sa.Integer(), nullable=True),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column(
            "reviewer_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reviewer_role", sa.String(32), nullable=False),
        sa.Column("subject_identity_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("document_id", "rev", name="pk_doc_identity_attestation"),
        sa.ForeignKeyConstraint(
            ["document_id", "prev_rev"],
            ["document_identity_attestations.document_id", "document_identity_attestations.rev"],
            name="fk_doc_attestation_prev_rev",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "document_sha256"],
            ["claim_documents.id", "claim_documents.sha256"],
            name="fk_doc_attestation_doc_sha256",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(rev = 1 AND prev_rev IS NULL) OR (rev > 1 AND prev_rev IS NOT NULL AND prev_rev = rev - 1)",
            name="chk_doc_attestation_rev_seq",
        ),
        sa.CheckConstraint(
            "result = 'CONFIRMED' OR (result IN ('MISMATCH', 'UNCERTAIN') AND reason_code IS NOT NULL AND remarks IS NOT NULL)",
            name="chk_doc_attestation_reason_remarks",
        ),
    )

    # 5. Append-only enforcement for document_identity_attestations and immutability for claim_documents
    if is_pg:
        op.execute(
            """
            CREATE OR REPLACE TRIGGER trg_no_update_delete_document_identity_attestations
            BEFORE UPDATE OR DELETE ON document_identity_attestations
            FOR EACH ROW EXECUTE FUNCTION trg_prevent_update_delete_truncate();
            """
        )
        op.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_rw') THEN
                    REVOKE UPDATE, DELETE, TRUNCATE ON document_identity_attestations FROM app_rw;
                END IF;
            END $$;
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION trg_prevent_doc_file_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.sha256 <> OLD.sha256 OR NEW.storage_key <> OLD.storage_key OR NEW.claim_id <> OLD.claim_id THEN
                    RAISE EXCEPTION 'File-defining columns (sha256, storage_key, claim_id) on claim_documents are strictly immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE OR REPLACE TRIGGER trg_no_mutate_doc_file
            BEFORE UPDATE ON claim_documents
            FOR EACH ROW EXECUTE FUNCTION trg_prevent_doc_file_mutation();
            """
        )
    else:
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_no_update_document_identity_attestations
            BEFORE UPDATE ON document_identity_attestations
            BEGIN
                SELECT RAISE(ABORT, 'Table document_identity_attestations is append-only: UPDATE is prohibited');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_no_delete_document_identity_attestations
            BEFORE DELETE ON document_identity_attestations
            BEGIN
                SELECT RAISE(ABORT, 'Table document_identity_attestations is append-only: DELETE is prohibited');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_no_mutate_doc_file
            BEFORE UPDATE ON claim_documents
            BEGIN
                SELECT RAISE(ABORT, 'File-defining columns (sha256, storage_key, claim_id) on claim_documents are strictly immutable')
                WHERE NEW.sha256 <> OLD.sha256 OR NEW.storage_key <> OLD.storage_key OR NEW.claim_id <> OLD.claim_id;
            END;
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("DROP TRIGGER IF EXISTS trg_no_mutate_doc_file ON claim_documents;")
        op.execute("DROP FUNCTION IF EXISTS trg_prevent_doc_file_mutation();")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_no_update_delete_document_identity_attestations ON document_identity_attestations;"
        )
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_no_mutate_doc_file;")
        op.execute("DROP TRIGGER IF EXISTS trg_no_update_document_identity_attestations;")
        op.execute("DROP TRIGGER IF EXISTS trg_no_delete_document_identity_attestations;")

    op.drop_table("document_identity_attestations")

    with op.batch_alter_table("claim_documents") as batch_op:
        batch_op.drop_constraint("chk_claim_document_invalid_reason_remarks", type_="check")
        batch_op.drop_constraint("uq_claim_document_id_sha256", type_="unique")

    with op.batch_alter_table("required_document_types") as batch_op:
        batch_op.drop_column("is_identity_bearing")

    with op.batch_alter_table("customers") as batch_op:
        batch_op.drop_column("identity_version")

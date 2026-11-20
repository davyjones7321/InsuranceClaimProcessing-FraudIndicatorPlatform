"""Integration test verifying that Alembic migrations apply on an empty DB and cleanly roll back."""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def test_alembic_migrations_upgrade_and_downgrade(tmp_path: Path) -> None:
    """Alembic migrations must apply cleanly to head and downgrade completely to base."""
    # Use isolated in-memory or file-based sqlite for migration test
    db_file = tmp_path / "test_migrations.db"
    migration_db_url = f"sqlite:///{db_file.as_posix()}"

    engine = create_engine(migration_db_url)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", migration_db_url)

    try:
        # 1. Upgrade to head
        command.upgrade(alembic_cfg, "head")

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "users" in tables
        assert "customers" in tables
        assert "policies" in tables
        assert "coverages" in tables
        assert "claims" in tables
        assert "audit_log" in tables
        assert "audit_chain_heads" in tables
        assert "coverage_ledger" in tables
        assert "document_identity_attestations" in tables
        assert "import_rejects" in tables
        assert "alembic_version" in tables

        claim_cols = {c["name"] for c in inspector.get_columns("claims")}
        assert "incident_group_id" in claim_cols
        assert "registered_on" in claim_cols
        assert "registered_tz" in claim_cols

        assessment_cols = {c["name"] for c in inspector.get_columns("assessments")}
        assert "ruleset_version" in assessment_cols
        assert "engine_version" in assessment_cols
        assert "ruleset_hash" in assessment_cols
        assert "inputs_snapshot" in assessment_cols

        val_run_cols = {c["name"] for c in inspector.get_columns("validation_runs")}
        assert "ruleset_hash" in val_run_cols
        assert "inputs_snapshot" in val_run_cols
        assert "calculation_trace" in val_run_cols

        risk_eval_cols = {c["name"] for c in inspector.get_columns("risk_evaluations")}
        assert "ruleset_hash" in risk_eval_cols
        assert "calculation_trace" in risk_eval_cols

        # 2. Downgrade to base
        command.downgrade(alembic_cfg, "base")

        inspector_after = inspect(engine)
        remaining_tables = set(inspector_after.get_table_names())
        # Only alembic_version might remain after base downgrade
        remaining_business_tables = remaining_tables - {"alembic_version"}
        assert not remaining_business_tables, (
            f"Tables left after downgrade: {remaining_business_tables}"
        )

        # 3. Upgrade back to head to verify re-apply
        command.upgrade(alembic_cfg, "head")
        inspector_reapplied = inspect(engine)
        reapplied_tables = set(inspector_reapplied.get_table_names())
        assert "users" in reapplied_tables
        assert "claims" in reapplied_tables
    finally:
        engine.dispose()

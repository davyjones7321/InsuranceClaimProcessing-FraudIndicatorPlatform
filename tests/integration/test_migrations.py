"""Integration test verifying that Alembic migrations apply on an empty DB and cleanly roll back."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


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


def test_alembic_postgres_app_rw_append_only_enforcement() -> None:
    """Build schema ONLY from Alembic migrations on PostgreSQL, connect as app_rw,
    and prove UPDATE, DELETE, TRUNCATE fail on audit_log and protected tables.
    """
    env_db = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    require_pg = os.environ.get("REQUIRE_POSTGRES", "0").lower() in ("1", "true", "yes")
    if not require_pg and (not env_db or "postgresql" not in env_db):
        pytest.skip("PostgreSQL required for migration app_rw append-only test")

    admin_db_url = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
    try:
        admin_engine = create_engine(admin_db_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"PostgreSQL server unreachable: {exc}")

    test_db_name = "test_alembic_app_rw_ci"
    with admin_engine.connect() as conn:
        conn.execute(
            text(f"DROP DATABASE IF EXISTS {test_db_name} WITH (FORCE);")
            if conn.dialect.server_version_info >= (13,)
            else text(f"DROP DATABASE IF EXISTS {test_db_name};")
        )
        conn.execute(text(f"CREATE DATABASE {test_db_name};"))

    migrator_url = f"postgresql+psycopg://postgres:postgres@localhost:5432/{test_db_name}"
    migrator_engine = create_engine(migrator_url)

    try:
        # 1. Run deploy/init-db.sql on empty database
        with open("deploy/init-db.sql", encoding="utf-8") as f:
            init_sql = f.read()
        with migrator_engine.connect() as conn:
            conn.execute(text(init_sql))
            conn.commit()

        # 2. Build schema ONLY from Alembic migrations
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", migrator_url)
        command.upgrade(alembic_cfg, "head")

        # 3. Connect as app_rw
        app_rw_url = f"postgresql+psycopg://app_rw:app_rw_secure_pass@localhost:5432/{test_db_name}"
        app_rw_engine = create_engine(app_rw_url)

        protected_tables = [
            "audit_log",
            "claim_status_history",
            "decisions",
            "investigation_findings",
            "coverage_ledger",
            "access_log",
        ]

        with app_rw_engine.connect() as rw_conn:
            for tbl in protected_tables:
                # A. Attempt UPDATE
                with pytest.raises(Exception, match=r"(permission denied|append-only|prohibited)"):
                    rw_conn.execute(text(f"UPDATE {tbl} SET id = id"))
                    rw_conn.commit()
                rw_conn.rollback()

                # B. Attempt DELETE
                with pytest.raises(Exception, match=r"(permission denied|append-only|prohibited)"):
                    rw_conn.execute(text(f"DELETE FROM {tbl}"))
                    rw_conn.commit()
                rw_conn.rollback()

                # C. Attempt TRUNCATE
                with pytest.raises(Exception, match=r"(permission denied|append-only|prohibited)"):
                    rw_conn.execute(text(f"TRUNCATE TABLE {tbl}"))
                    rw_conn.commit()
                rw_conn.rollback()

        app_rw_engine.dispose()
    finally:
        migrator_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(
                text(f"DROP DATABASE IF EXISTS {test_db_name} WITH (FORCE);")
                if conn.dialect.server_version_info >= (13,)
                else text(f"DROP DATABASE IF EXISTS {test_db_name};")
            )
        admin_engine.dispose()

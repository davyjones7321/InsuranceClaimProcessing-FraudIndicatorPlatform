"""Integration test verifying that Alembic migrations apply on an empty DB and cleanly roll back."""

import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from alembic import command


def test_alembic_migrations_upgrade_and_downgrade(tmp_path: Path) -> None:
    """Alembic migrations must apply cleanly to head and downgrade completely to base."""
    # Use isolated in-memory or file-based sqlite for migration test
    db_file = tmp_path / "test_migrations.db"
    migration_db_url = f"sqlite:///{db_file.as_posix()}"

    engine = create_engine(migration_db_url)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", migration_db_url.replace("%", "%%"))

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
    test_db_url = os.environ.get("TEST_DATABASE_URL")
    if not test_db_url or "postgresql" not in test_db_url:
        pytest.fail("TEST_DATABASE_URL environment variable must be set to a valid PostgreSQL URL")

    parsed_url = make_url(test_db_url)
    admin_db_url = parsed_url.set(database="postgres").render_as_string(hide_password=False)
    try:
        admin_engine = create_engine(admin_db_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.fail(f"PostgreSQL server unreachable at {admin_db_url}: {exc}")

    test_db_name = "test_alembic_app_rw_ci"
    with admin_engine.connect() as conn:
        conn.execute(
            text(f"DROP DATABASE IF EXISTS {test_db_name} WITH (FORCE);")
            if conn.dialect.server_version_info >= (13,)
            else text(f"DROP DATABASE IF EXISTS {test_db_name};")
        )
        conn.execute(text(f"CREATE DATABASE {test_db_name};"))

    try:
        # 1. Run deploy/init-db.sql on empty database using admin connection
        admin_test_url = parsed_url.set(database=test_db_name).render_as_string(hide_password=False)
        admin_test_engine = create_engine(admin_test_url)
        with open("deploy/init-db.sql", encoding="utf-8") as f:
            init_sql = f.read()
        with admin_test_engine.connect() as conn:
            conn.execute(text(init_sql))
            conn.commit()
        admin_test_engine.dispose()

        # 2. Build schema ONLY from Alembic migrations as migrator
        migrator_url = parsed_url.set(
            username="migrator", password="migrator_secure_pass", database=test_db_name
        ).render_as_string(hide_password=False)
        migrator_engine = create_engine(migrator_url)

        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", migrator_url.replace("%", "%%"))
        command.upgrade(alembic_cfg, "head")

        # 3. Connect as app_rw
        app_rw_url = parsed_url.set(
            username="app_rw", password="app_rw_secure_pass", database=test_db_name
        ).render_as_string(hide_password=False)
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
        migrator_engine.dispose()
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                text(f"DROP DATABASE IF EXISTS {test_db_name} WITH (FORCE);")
                if conn.dialect.server_version_info >= (13,)
                else text(f"DROP DATABASE IF EXISTS {test_db_name};")
            )
        admin_engine.dispose()


def test_alembic_postgres_upgrade_downgrade_upgrade() -> None:
    """Test alembic upgrade head, downgrade base, upgrade head on PostgreSQL 16."""
    test_db_url = os.environ.get("TEST_DATABASE_URL")
    if not test_db_url or "postgresql" not in test_db_url:
        pytest.fail("TEST_DATABASE_URL environment variable must be set to a valid PostgreSQL URL")

    parsed_url = make_url(test_db_url)
    admin_db_url = parsed_url.set(database="postgres").render_as_string(hide_password=False)
    try:
        admin_engine = create_engine(admin_db_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.fail(f"PostgreSQL server unreachable at {admin_db_url}: {exc}")

    test_db_name = "test_alembic_up_down_up_ci"
    with admin_engine.connect() as conn:
        conn.execute(
            text(f"DROP DATABASE IF EXISTS {test_db_name} WITH (FORCE);")
            if conn.dialect.server_version_info >= (13,)
            else text(f"DROP DATABASE IF EXISTS {test_db_name};")
        )
        conn.execute(text(f"CREATE DATABASE {test_db_name};"))

    try:
        admin_test_url = parsed_url.set(database=test_db_name).render_as_string(hide_password=False)
        admin_test_engine = create_engine(admin_test_url)
        with open("deploy/init-db.sql", encoding="utf-8") as f:
            init_sql = f.read()
        with admin_test_engine.connect() as conn:
            conn.execute(text(init_sql))
            conn.commit()
        admin_test_engine.dispose()

        migrator_url = parsed_url.set(
            username="migrator", password="migrator_secure_pass", database=test_db_name
        ).render_as_string(hide_password=False)
        migrator_engine = create_engine(migrator_url)
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", migrator_url.replace("%", "%%"))

        # 1. Upgrade to head
        command.upgrade(alembic_cfg, "head")
        inspector = inspect(migrator_engine)
        assert "audit_log" in inspector.get_table_names()
        assert "claims" in inspector.get_table_names()

        # 2. Downgrade to base (must cleanly drop triggers including trg_no_truncate_*)
        command.downgrade(alembic_cfg, "base")
        inspector_after = inspect(migrator_engine)
        remaining = set(inspector_after.get_table_names()) - {"alembic_version"}
        assert not remaining, f"Tables remaining after downgrade: {remaining}"

        # 3. Upgrade back to head
        command.upgrade(alembic_cfg, "head")
        inspector_final = inspect(migrator_engine)
        assert "audit_log" in inspector_final.get_table_names()
        assert "claims" in inspector_final.get_table_names()

        migrator_engine.dispose()
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                text(f"DROP DATABASE IF EXISTS {test_db_name} WITH (FORCE);")
                if conn.dialect.server_version_info >= (13,)
                else text(f"DROP DATABASE IF EXISTS {test_db_name};")
            )
        admin_engine.dispose()


def test_alembic_config_escapes_percent_in_database_url_with_password() -> None:
    """Alembic config uses ConfigParser interpolation by default. Database URLs containing
    '%' in passwords must be escaped as '%%' before calling set_main_option(), allowing
    get_main_option() to retrieve the exact original unescaped URL without ValueError.
    """
    raw_url = "postgresql+psycopg://migrator:p%40ss%25word%21@localhost:5432/insurance_claims"
    alembic_cfg = Config("alembic.ini")

    # 1. Unescaped set_main_option fails due to configparser interpolation syntax validation
    with pytest.raises(ValueError, match=r"invalid interpolation syntax"):
        alembic_cfg.set_main_option("sqlalchemy.url", raw_url)

    # 2. Escaping '%' as '%%' passes validation and correctly recovers unescaped URL
    escaped_url = raw_url.replace("%", "%%")
    alembic_cfg.set_main_option("sqlalchemy.url", escaped_url)
    assert alembic_cfg.get_main_option("sqlalchemy.url") == raw_url

    # 3. Offline migration executes successfully with the escaped URL
    command.upgrade(alembic_cfg, "head", sql=True)

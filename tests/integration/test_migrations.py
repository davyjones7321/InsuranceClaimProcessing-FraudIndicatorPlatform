"""Integration test verifying that Alembic migrations apply on an empty DB and cleanly roll back."""

import os

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def test_alembic_migrations_upgrade_and_downgrade() -> None:
    """Alembic migrations must apply cleanly to head and downgrade completely to base."""
    # Use isolated in-memory or file-based sqlite for migration test
    migration_db_url = "sqlite:///test_migrations.db"
    if os.path.exists("test_migrations.db"):
        os.remove("test_migrations.db")

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
        assert "alembic_version" in tables

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
        if os.path.exists("test_migrations.db"):
            os.remove("test_migrations.db")

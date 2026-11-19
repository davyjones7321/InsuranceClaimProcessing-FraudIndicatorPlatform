-- Database initialization script for PostgreSQL 16
-- Sets up migrator (DDL) and app_rw (DML with append-only restrictions on immutable tables)

-- Create roles if they don't exist
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'migrator') THEN
        CREATE ROLE migrator WITH LOGIN PASSWORD 'migrator_secure_pass';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_rw') THEN
        CREATE ROLE app_rw WITH LOGIN PASSWORD 'app_rw_secure_pass';
    END IF;
END $$;

-- Grant schema privileges
GRANT ALL ON SCHEMA public TO migrator;
GRANT USAGE ON SCHEMA public TO app_rw;

-- Default privileges for app_rw on future tables created by migrator
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;

ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_rw;

-- Append-only enforcement: Revoke UPDATE and DELETE from app_rw on append-only tables
-- (Run after migrations or in migration hooks)
-- REVOKE UPDATE, DELETE ON audit_log, claim_status_history, decisions, investigation_findings, coverage_ledger FROM app_rw;

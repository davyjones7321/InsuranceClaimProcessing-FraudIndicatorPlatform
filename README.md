# Insurance Claim Processing & Fraud Indicator Platform (v1)

A deterministic, rule-driven insurance claim processing and fraud indicator platform built with Python 3.12+, FastAPI, SQLAlchemy 2.x, PostgreSQL 16, and Jinja2 templates.

## Architecture

- **Engine (`app/engine/`):** Pure deterministic logic with zero side effects, no database calls, no clock reads, and no random numbers.
- **Audit (`app/audit/`):** Append-only independent hash chains (per-claim and system) secured with HMAC-SHA256.
- **Security (`app/security/`):** Argon2 password hashing, server-side RBAC on every route, session management, and object-level permissions.
- **Rules (`app/rules/`):** Versioned YAML rule specifications.

## Running Locally

1. Create and activate virtual environment:
   ```bash
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
2. Install dependencies:
   ```bash
   pip install -e ".[dev]"
   ```
3. Start database with Docker:
   ```bash
   docker compose -f deploy/docker-compose.yml up -d db
   ```
4. Run migrations:
   ```bash
   alembic upgrade head
   ```
5. Run tests:
   ```bash
   pytest
   ```

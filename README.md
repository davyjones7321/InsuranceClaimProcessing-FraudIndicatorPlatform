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


## Customer account registration (prototype)

The browser sign-in page at `/ui/login` links to `/ui/register`. Public registration creates a customer profile and a linked user account in the existing PostgreSQL database. Passwords are stored using the existing Argon2id hasher, and the new customer is signed in after successful registration.

Required registration details: full name, email, password (minimum 12 characters), date of birth, phone, address, and state/region. Email addresses must be unique. Public registration always assigns the `customer` role; it cannot create staff, manager, or administrator accounts. Privileged accounts must be provisioned through a separate administrative process.

The JSON API is `POST /api/auth/register`. It returns the created user profile; clients can then use `POST /api/auth/login` to establish a session. No new database or schema migration is required: registration uses the existing `customers` and `users` tables.

This is a prototype self-service flow. Email ownership verification, password recovery, and production account-approval workflows are not implemented and must be addressed before production use.

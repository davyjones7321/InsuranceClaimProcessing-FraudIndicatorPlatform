# Insurance Claim Processing & Fraud Indicator Platform

A prototype insurance-claims workflow and fraud-indicator platform built with Python 3.12+, FastAPI, SQLAlchemy, PostgreSQL 16, and Jinja2.

> **DEMO DATA:** The Docker deployment automatically loads 50 fictional claims, 25 fictional customers, sample policies, workflow records, and demo accounts. All names, contact details, policies, and claim events are synthetic. Do not use demo credentials or demo data in production.

## Run with Docker (recommended)

### Requirements
- Docker Desktop (or Docker Engine) with Docker Compose v2.
- Ports 80, 443, and 5432 available.
- At least 2 GB of memory available to Docker.

### Start the application

Run this from the repository root:
```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Compose starts PostgreSQL, waits for its health check, applies Alembic migrations, runs the demo seed, and then starts the application and Caddy.

- Application: http://localhost
- Health check: http://localhost/healthz
- API docs: http://localhost/docs

Check containers and logs:
```bash
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f app
docker compose -f deploy/docker-compose.yml logs migration
docker compose -f deploy/docker-compose.yml logs demo_seed
```

Stop the stack without deleting the database:
```bash
docker compose -f deploy/docker-compose.yml down
```

**Do not add `-v`** unless you intentionally want to delete the PostgreSQL volume and all data.

### Demo accounts

All demo accounts use this password:
```text
DemoPassword123!
```

| Role | Login |
|---|---|
| Administrator | admin@example.com |
| Manager | manager@example.com |
| Investigator | investigator@example.com |
| Assessor | assessor@example.com |
| Staff | staff@example.com |
| Customer | customer1@example.com |
| Customer (second profile) | customer2@example.com |

Use the role accounts to explore role-specific dashboards and claim queues. Customer accounts only see claims belonging to their customer profile; investigator views are scoped to assigned investigations.

These are shared, intentionally simple demonstration credentials. Change or disable them before any deployment outside a trusted demo environment.

### Included demo dataset
- Exactly 50 synthetic claims distributed across registration, document-pending, validation, risk review, investigation, assessment, approval, rejection, information-requested, and withdrawn states.
- 25 fictional customer profiles and sample HEALTH, VEHICLE, and PROPERTY policies.
- Related validation, risk-indicator, investigation, assessment, decision, and audit records where applicable.
- Demo accounts covering all six application roles.
- Illustrative review flags; these are not real fraud findings.

The seed uses deterministic IDs and never truncates tables. It creates the demo claim batch only when the claims table is empty. If claims already exist, it leaves them untouched. Re-running the seed does not add another batch.

### Reset to a clean demo database (destructive)

Only use this when you intentionally want to discard the local database and recreate the demo:
```bash
docker compose -f deploy/docker-compose.yml down -v
docker compose -f deploy/docker-compose.yml up -d --build
```

The `down -v` command permanently deletes the PostgreSQL data volume.

## Build and run the Docker image manually

Compose is recommended because the application requires PostgreSQL and migrations. To build the web application image from the repository root:
```bash
docker build -f deploy/Dockerfile -t insurance-claim-platform:demo .
```

The image contains the application, not PostgreSQL. Start a PostgreSQL 16 database separately, then run migrations and (for an empty demo database) the seed command from the same image. Replace the example credentials, host, and secrets with values for your environment:
```bash
docker run --rm --network YOUR_DOCKER_NETWORK \\
  -e ENV=development \\
  -e DATABASE_URL='postgresql+psycopg://APP_USER:APP_PASSWORD@YOUR_DB_HOST:5432/insurance_claims' \\
  -e SECRET_KEY='replace-with-a-local-secret-of-at-least-32-characters' \\
  -e AUDIT_HMAC_KEY='replace-with-a-different-local-secret-of-at-least-32-characters' \\
  insurance-claim-platform:demo alembic upgrade head

docker run --rm --network YOUR_DOCKER_NETWORK \\
  -e ENV=development \\
  -e DATABASE_URL='postgresql+psycopg://APP_USER:APP_PASSWORD@YOUR_DB_HOST:5432/insurance_claims' \\
  -e SECRET_KEY='replace-with-a-local-secret-of-at-least-32-characters' \\
  -e AUDIT_HMAC_KEY='replace-with-a-different-local-secret-of-at-least-32-characters' \\
  insurance-claim-platform:demo python -m scripts.seed
```

Then start the web container:
```bash
docker run -d --name claim-platform-app -p 8000:8000 \\
  -e ENV=development \\
  -e DATABASE_URL='postgresql+psycopg://APP_USER:APP_PASSWORD@YOUR_DB_HOST:5432/insurance_claims' \\
  -e SECRET_KEY='replace-with-a-local-secret-of-at-least-32-characters' \\
  -e AUDIT_HMAC_KEY='replace-with-a-different-local-secret-of-at-least-32-characters' \\
  insurance-claim-platform:demo
```

Open http://localhost:8000. The standalone image does not include the Caddy reverse proxy; the Compose setup provides it.

## Run locally without Docker

1. Create and activate a virtual environment:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
2. Install dependencies:
   ```bash
   pip install -e ".[dev]"
   ```
3. Start PostgreSQL:
   ```bash
   docker compose -f deploy/docker-compose.yml up -d db
   ```
4. Configure `DATABASE_URL` for the local PostgreSQL instance, then migrate and seed:
   ```bash
   alembic upgrade head
   python -m scripts.seed
   ```
5. Start the API:
   ```bash
   uvicorn app.main:app --reload
   ```
6. Run tests:
   ```bash
   pytest
   ```

## Architecture
- **Engine (`app/engine/`):** Deterministic business logic.
- **Audit (`app/audit/`):** Append-only hash-chained audit records.
- **Security (`app/security/`):** Password hashing, server-side role-based access control, session management, and object-level permissions.
- **Rules (`app/rules/`):** Versioned YAML rule specifications.
- **Database (`app/db/`):** SQLAlchemy models and PostgreSQL persistence.

## Prototype limitations

This repository is a demonstration prototype, not a production-ready insurance system. Demo data is fictional; risk flags are illustrative and are not validated fraud findings. Review authentication, secrets, email verification, account lifecycle, document handling, monitoring, and business rules before real deployment. Docker Compose defaults and demo credentials are intended for local demonstration only.

## Customer account registration (prototype)

The browser sign-in page at `/ui/login` links to `/ui/register`. Public registration creates a customer profile and linked user account. It requires a full name, email, password (minimum 12 characters), date of birth, phone, address, and state/region. Email addresses must be unique, and public registration always assigns the customer role. Privileged accounts must be provisioned separately.

The JSON endpoint is `POST /api/auth/register`. Email ownership verification, password recovery, and production account-approval workflows are not implemented.

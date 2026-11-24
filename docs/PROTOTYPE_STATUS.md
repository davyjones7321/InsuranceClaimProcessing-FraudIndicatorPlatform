# Prototype Status and Known Limitations

**Status date:** 2026-11-24  
**Target:** Local demonstration prototype  
**Not approved for:** Real insurance claims, customer documents, financial decisions, or production use

This document separates the prototype target from production-readiness requirements. A known limitation is not evidence that the affected workflow works; items marked unverified still need testing.

## 1. Current implementation

### Backend
The application is a Python 3.12 / FastAPI service using SQLAlchemy, Alembic, and PostgreSQL. The repository includes authentication and role/claim authorization; customer, policy, coverage, and claim workflows; document upload/review; validation rules and pilot risk indicators; assessment, investigation, and human decisions; analytics, audit records, health/readiness endpoints; PostgreSQL migrations and deterministic seed data.

This inventory does not mean every workflow has passed an end-to-end browser test.

### Frontend
The UI remains server-rendered with FastAPI and Jinja2 templates. Commit `7687cb4` refreshed the shared layout and styling with a restrained forest/ivory/lime visual system, updated typography, responsive navigation, cards, tables, forms, and reduced-motion support.

There is no separate Next.js/React frontend. Keeping the current server-rendered UI avoids introducing a second application and API-authentication integration solely for this prototype. A separate frontend can be considered later if richer client-side interactions become necessary.

**Visual/browser verification:** Not yet completed. Passing CI static checks does not prove every page renders correctly at desktop and mobile sizes.

## 2. Local prototype startup

Requirements: Docker Desktop with Docker Compose and a working Docker engine.

From the repository root:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
```

The Compose stack defines PostgreSQL, a migration container, the application, and Caddy. The application waits for PostgreSQL health and successful migration completion. Caddy exposes the UI on local port 80.

Populate deterministic demo data:

```bash
docker compose -f deploy/docker-compose.yml exec app python scripts/seed.py
```

The seed script creates demo users for available roles with the shared development password `DevPassword123!`. These credentials and all seeded data are for isolated local demonstration only. Do not reuse them outside the local prototype.

Open:
- UI: http://localhost/
- Liveness: http://localhost/healthz
- Readiness: http://localhost/readyz

If startup fails:

```bash
docker compose -f deploy/docker-compose.yml logs --tail=200 db migration app caddy
```

Stop the stack:

```bash
docker compose -f deploy/docker-compose.yml down
```

To delete the local database and uploaded-document volumes and start clean, use `docker compose -f deploy/docker-compose.yml down -v`. This permanently deletes prototype data.

## 3. Known prototype limitations and open verification

| Area | Current state | Required follow-up |
|---|---|---|
| Database | Compose provisions local PostgreSQL with development credentials and separate migrator/app roles. | Verify clean first boot, migrations, seeding, and restart persistence on the target machine. |
| Demo accounts | Seed script uses a shared known development password. | Keep data local; replace credentials before any shared demo. |
| UI | Shared theme updated; Jinja2 pages retained. | Browser-test login, claims, claim detail, registration, documents, assessment, investigation, decision, analytics, and responsive layouts. |
| Workflows | Backend routes/services and automated tests exist. | Exercise representative user journeys through the browser and record pass/fail evidence. |
| CSRF | UI forms have token validation in relevant POST handlers; global middleware has a UI-path exception. | Review and consolidate CSRF protection before any network-exposed use. |
| Assessment formulas | Calculation specification is illustrative; values are not insurer-approved. | Obtain signed business/actuarial specification and approved golden test vectors. |
| Risk indicators | Pilot indicators only; not statistically validated or calibrated. | Validate against representative labelled data and obtain risk-owner approval. |
| Upload scanning | Antivirus is disabled by default. | Configure malware scanning before accepting real documents. |
| Audit anchoring | HMAC audit-chain implementation exists; external immutable storage is not demonstrated as configured. | Configure and independently verify external Object Lock anchoring and credentials. |
| Secrets | Compose contains development-only database passwords and application secrets. | Use generated secrets and a secrets manager for any shared environment. |
| Deployment | CI builds and publishes an image. Staging and production are gated and were skipped in the prototype workflow. | Local Compose is the current deployment target; no cloud deployment is claimed. |
| Recovery | Backup/restore tooling and a CI drill exist. | Verify local volume backup/recovery and document the actual recovery procedure for the demo host. |

## 4. Production handover requirements

Before real-world use, the next team must:
1. Resolve business decisions and obtain formal approval for claim rules, coverage-limit behaviour, and assessment calculations.
2. Validate and calibrate risk indicators using representative, appropriately governed data; document false-positive/false-negative performance and human oversight.
3. Complete security review, including CSRF coverage, authentication/session controls, authorization testing, malware scanning, dependency/image scanning policy, and penetration testing.
4. Replace development credentials and secrets; enforce TLS and secure proxy configuration.
5. Provision durable, access-controlled document storage, retention/deletion policy, encryption, backups, and restore drills.
6. Configure and independently verify immutable external audit anchoring, key custody, and audit verification procedures.
7. Establish production monitoring, alert ownership, incident response, disaster recovery, and rollback procedures.
8. Complete accessibility, browser compatibility, load, and end-to-end workflow testing.
9. Obtain legal, compliance, privacy, and business-owner sign-off for the intended jurisdiction and use.

## 5. Verification evidence

For UI redesign commit `7687cb4c9b41e92681debb7c6c6abcd88ef2177f`, the CI code-quality/SAST job passed. The container build-and-publish job passed. Staging and production deployment jobs were skipped by design.

The PostgreSQL integration, migration, full test-suite, and backup/restore job was still running when this document was prepared. Update this section with its final result before calling the prototype test cycle complete.

- CI run: https://github.com/davyjones7321/InsuranceClaimProcessing-FraudIndicatorPlatform/actions/runs/45385633144
- CD run: https://github.com/davyjones7321/InsuranceClaimProcessing-FraudIndicatorPlatform/actions/runs/45385633108

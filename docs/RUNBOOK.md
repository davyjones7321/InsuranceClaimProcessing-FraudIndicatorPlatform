# Operations Runbook

## 1. Local Development Setup

```bash
# 1. Start database
docker compose -f deploy/docker-compose.yml up -d db

# 2. Run migrations
alembic upgrade head

# 3. Create initial administrator
python scripts/create_admin.py --email admin@example.com --password "SecureAdminPassword123!"

# 4. Start local development server
uvicorn app.main:app --reload --port 8000
```

## 2. Production Deployment & Zero-Downtime Rollout

1. **Pre-flight checks:**
   - Confirm CI pipeline is green.
   - Verify active ruleset hash against the database registry.
2. **Execute migrations:**
   Run migrations as a distinct release step using the dedicated `migrator` role:
   ```bash
   alembic upgrade head
   ```
3. **Deploy application container:**
   Start application containers with non-root user `appuser`.
4. **Health verification:**
   Verify `/healthz` and `/readyz` endpoints return HTTP 200.

## 3. Rollback Procedure

If a deployment fails:
1. Re-deploy the previously tagged Docker image container.
2. If database schema was altered, execute the reversible migration downgrade:
   ```bash
   alembic downgrade -1
   ```

## 4. Database Backup & Restore Drill

- **Daily automated backup:**
  ```bash
  pg_dump -U postgres -d insurance_claims -F c -b -v -f /backups/claims_backup_$(date +%Y%m%d).dump
  ```
- **Restore verification drill:**
  ```bash
  createdb -U postgres insurance_claims_drill
  pg_restore -U postgres -d insurance_claims_drill -v /backups/claims_backup_latest.dump
  python scripts/verify_audit.py --db-url postgresql+psycopg://postgres:password@localhost:5432/insurance_claims_drill
  dropdb -U postgres insurance_claims_drill
  ```

## 5. Audit Chain Verification & Incident Response

Run scheduled verification nightly (incremental) and weekly (full):
```bash
python scripts/verify_audit.py --chain all
```
- **In the event of a chain break:**
  1. Never alter or overwrite rows in `audit_log` or `audit_chain_heads`.
  2. Treat the incident as a critical security breach.
  3. Record the incident in `verification_runs`.
  4. Resumption requires an administrator-signed `CHAIN_BREAK_ACKNOWLEDGED` event.

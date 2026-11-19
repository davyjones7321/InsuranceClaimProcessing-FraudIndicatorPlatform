# Production Operations Runbook

This runbook documents operational procedures for deploying, maintaining, backing up, restoring, and troubleshooting the Insurance Claim Processing & Fraud Indicator Platform.

---

## 1. Local Development & Staging Setup

```bash
# 1. Start managed database container
docker compose -f deploy/docker-compose.yml up -d db

# 2. Run database migrations as migrator role
alembic upgrade head

# 3. Bootstrap initial administrator account
python scripts/create_admin.py --email admin@example.com --password "SecureAdminPassword123!"

# 4. (Optional) Populate deterministic demo dataset
python scripts/seed.py

# 5. Start application server
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 2. Production Deployment & Zero-Downtime Rollout

### 2.1 Pre-Flight Verification
1. Confirm CI pipeline is green on target commit.
2. Verify active ruleset hash against the database registry:
   ```bash
   python -c "from app.rules.loader import ACTIVE_RULESET_SHA256; print('Active SHA256:', ACTIVE_RULESET_SHA256)"
   ```
3. Verify environment variables:
   - `ENV=production`
   - `DEBUG=false`
   - `DATABASE_URL` (PostgreSQL with `app_rw` credentials)
   - `MIGRATOR_DATABASE_URL` (PostgreSQL with `migrator` credentials)
   - `SECRET_KEY` (minimum 32 random characters)
   - `AUDIT_HMAC_KEY` (minimum 32 random characters)

### 2.2 Release Sequence
1. **Database Migration Step:**
   Execute Alembic migration prior to traffic shifting using `migrator` credentials:
   ```bash
   alembic upgrade head
   ```
2. **Container Rolling Update:**
   Deploy new application containers running as unprivileged user `appuser` (UID 1000).
   Gunicorn manages Uvicorn worker processes with graceful worker restart (`kill -HUP <master_pid>`).
3. **Health Verification:**
   Confirm probes return HTTP 200 before routing live traffic:
   - Liveness probe: `curl -f http://localhost:8000/healthz`
   - Readiness probe: `curl -f http://localhost:8000/readyz`

---

## 3. Rollback Procedure

If defects or regressions occur after a release:
1. **Revert Application Image:**
   Immediately re-deploy the previous known-good Docker image tag.
2. **Database Schema Rollback:**
   If the release included an Alembic migration that must be rolled back:
   ```bash
   alembic downgrade -1
   ```
   *Note:* Ensure the migration script defines a valid `downgrade()` implementation.
3. **Readiness Check:**
   Verify `/readyz` returns HTTP 200 after reverting.

---

## 4. Secret Rotation Procedures

### 4.1 Session `SECRET_KEY` Rotation
1. Update `SECRET_KEY` in environment configuration / secrets manager.
2. Existing user sessions will be invalidated upon rotation, requiring users to log in again.
3. Perform rolling container restart.

### 4.2 Audit Chain `AUDIT_HMAC_KEY` Rotation
1. The HMAC key signs each link in the hash chain:
   $$\text{entry\_hash} = \text{HMAC-SHA256}(K, \text{prev\_hash} \parallel \text{seq} \parallel \text{canonical\_payload})$$
2. Prior to rotating `AUDIT_HMAC_KEY`, run a full audit verification:
   ```bash
   python scripts/verify_audit.py --chain all
   ```
3. Record an anchor verification checkpoint in `verification_runs`.
4. Deploy the updated `AUDIT_HMAC_KEY` into production environment variables.
5. Record an audited system event `AUDIT_KEY_ROTATED` under the new key.

---

## 5. User Management & Access Control

### 5.1 Bootstrap Initial Administrator
Run `scripts/create_admin.py` to create the initial administrative account without default credentials:
```bash
python scripts/create_admin.py --email admin@insurance.example --password "ComplexAdminPassphrase2026!"
```

### 5.2 Adding Internal Staff Users
Administrators create internal staff accounts via API or admin panel:
- Required roles: `STAFF`, `INVESTIGATOR`, `ASSESSOR`, `DECISION_MAKER`, `MANAGER`.
- Minimum password length: 12 characters (hashed with argon2id).
- All user creation and role changes are recorded in the `audit_log` system chain.

---

## 6. Ruleset Governance & Activation

1. **Create New Ruleset Version:**
   Add new version file `app/rules/ruleset_v2.yaml` defining updated validation rules or risk indicators.
2. **Verify Ruleset Constraints:**
   - Float values are strictly prohibited (use integers or strings).
   - Run ruleset loader check:
     ```bash
     python -c "from app.rules.loader import load_ruleset; load_ruleset('app/rules/ruleset_v2.yaml')"
     ```
3. **Deploy & Activate:**
   Set `ACTIVE_RULESET_VERSION=v2` in configuration and restart service.
   Existing historical claims preserve the ruleset version under which they were evaluated (`claim.ruleset_version`).

---

## 7. Database Backup, Restore, and Verification Drills

### 7.1 Automated Daily Backups
The automated backup job runs daily at 02:00 UTC with a 30-day retention policy:
```bash
python scripts/backup_db.py --output /backups/
```
Or native PostgreSQL dump:
```bash
pg_dump -U postgres -d insurance_claims -F c -b -v -f /backups/claims_backup_$(date +%Y%m%d_%H%M%S).dump
```

### 7.2 Restore Verification Drill
Drills must be conducted monthly to verify data integrity and recovery time objectives:
```bash
python scripts/drill_backup_restore.py
```

### 7.3 Measured Restore Drill Evidence
Output captured from automated drill execution (`[measured]`):
```
======================================================================
INSURANCE CLAIM PLATFORM: BACKUP & RESTORE DRILL VERIFICATION
Working directory: C:\Users\DavyJ\AppData\Local\Temp\claims_drill_yf7xluyd
======================================================================
[Step 1/4] Initiating database backup...
SQLite backup successfully written to: ...\claims_backup_20261119_151328.sqlite
-> Backup completed in 0.0047s (782336 bytes)

[Step 2/4] Setting up drill target database...
SQLite database restored successfully to: ...\restored_drill.sqlite
-> Restore completed in 0.0031s

[Step 3/4] Verifying restored entity counts against target database...
  • Users:                  7
  • Customers:              25
  • Policies:               13
  • Coverages:              22
  • Claims:                 230
  • Decisions:              65
  • Investigation Findings: 40
  • Audit Log Entries:      1
  • Audit Chain Heads:      1

[Step 4/4] Verifying HMAC-SHA256 cryptographic audit chains on restored database...
-> Verified 1/1 audit chains in 0.0052s
-> 100% cryptographic integrity confirmed on restored backup.

======================================================================
DRILL SUMMARY [measured]:
  • Total Duration:         6.1070s
  • Backup Duration:        0.0047s (782336 bytes)
  • Restore Duration:       0.0031s
  • Audit Verify Duration:  0.0052s
  • Total Entities Restored:276
  • Chains Verified:        1/1 (100% valid)
======================================================================
```

---

## 8. Audit Chain Verification & Incident Response

### 8.1 Scheduled Verification
Run audit chain verification nightly (incremental) and weekly (full):
```bash
python scripts/verify_audit.py --chain all
```

### 8.2 Audit Breach Response Procedure
If verification reports `❌ FAIL: Chain broken at seq N`:
1. **Do not modify or delete rows:** Never edit `audit_log` or `audit_chain_heads`.
2. **Isolate incident:** Record the affected `chain_key`, `first_broken_seq`, and timestamps in incident management.
3. **Forensic investigation:** Compare database replication logs, transaction logs, and external anchor hashes to identify altered fields.
4. **Resumption:** Resuming audit writes on a broken chain requires administrative review and a signed `CHAIN_BREAK_ACKNOWLEDGED` event.

---

## 9. Common Failures & Troubleshooting

| Symptom | Probable Cause | Remediation |
|---|---|---|
| `/healthz` fails | Application process hung or crashed | Check container process status; review stderr logs. |
| `/readyz` returns 503 | Database unreachable or ruleset mismatch | Verify DB network route; verify file integrity of active ruleset. |
| 5xx error rate alert | Unhandled exception or DB pool exhaustion | Inspect structured JSON logs by `request_id`; check DB pool metrics. |
| Optimistic lock `409` | Concurrent update to claim status | Re-fetch claim state and re-submit if appropriate. |
| Login rate limit `429` | Brute force attempt or shared IP | Check IP access logs; account lockout resets after cooldown. |
| Audit chain verification fails | Data corruption or unauthorized DB update | Follow Section 8.2 incident response immediately. |

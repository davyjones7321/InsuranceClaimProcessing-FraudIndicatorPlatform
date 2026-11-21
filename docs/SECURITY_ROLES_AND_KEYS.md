# Security Roles, Cryptographic Keys & Escrow Architecture (Phase P7 Task 8)

This document establishes the least-privilege role separation, cryptographic key hierarchy, escrow protocols, and incident response procedures for the Insurance Claim Processing & Fraud Indicator Platform per Decisions D3, D9, and D10.

---

## 1. Database Role Separation & Least Privilege

The platform enforces database-level role segregation. The runtime application never connects with administrative, superuser, or DDL privileges.

| Database Role | Privileges | Scope & Target Tables | Environment / Connection String |
|---|---|---|---|
| **`migrator`** | DDL (`CREATE`, `ALTER`, `DROP`), `SELECT`, `INSERT`, `UPDATE` on `alembic_version` | Migration lifecycle only. Used exclusively during CI/CD deployment jobs. | `MIGRATOR_DATABASE_URL` |
| **`app_rw`** | DML (`SELECT`, `INSERT`, `UPDATE`, `DELETE`) on domain tables. **Append-only** on audit tables (`SELECT`, `INSERT` only on `audit_log`, `audit_chain_heads`, `access_log`, `job_runs`). `UPDATE`/`DELETE` strictly revoked. | Web application server (Gunicorn/Uvicorn workers). | `DATABASE_URL` |
| **`audit_verifier`** | `SELECT` (Read-only) across all tables, specifically `audit_log`, `audit_chain_heads`, `verification_runs`, and `access_log`. | Independent verifier scripts, automated compliance drills, and auditor consoles. | `AUDIT_VERIFIER_DATABASE_URL` |
| **`anchor_writer`** | Write access (`s3:PutObject`) strictly limited to the external anchor manifest repository. No database DDL/DML access. | Dedicated daily anchor cron runner (`scripts/anchor_audit.py`). | Dedicated IAM role / Anchor storage token |

---

## 2. Encryption Architecture

### 2.1 Encryption in Transit
- **External Web Traffic:** All external traffic terminates at reverse proxy (Caddy / ALB) enforcing **TLS 1.3** with strong cipher suites (`TLS_AES_128_GCM_SHA256`, `TLS_AES_256_GCM_SHA384`). Plain HTTP redirects to HTTPS automatically.
- **Database Connections:** All database connections require TLS (`sslmode=verify-full`). Unencrypted PostgreSQL connection attempts are refused at the database engine level.

### 2.2 Encryption at Rest
- **Database Engine:** Amazon RDS for PostgreSQL 16 encrypted at rest using AWS Key Management Service (AWS KMS) customer-managed keys (CMK) with automated annual rotation.
- **Document & File Storage:** Claim documents stored on encrypted EBS volumes and dedicated S3 buckets with server-side encryption (`aws:kms` or `AES256`).
- **Audit & Sensitive Attributes:** Free-text descriptions and PII are redacted or cryptographically hashed (`SHA-256`) prior to indexing in audit logs.

### 2.3 Backup Encryption
- Database backups created via `pg_dump` are compressed and encrypted using **AES-256-GCM** before persistence or transit to secondary backup buckets.
- Backup encryption keys are managed independently from primary application keys.

---

## 3. Cryptographic Key Management & Escrow

### 3.1 Key Inventory

| Key Identifier | Purpose | Storage Medium | Lifecycle & Rotation |
|---|---|---|---|
| **`SECRET_KEY`** | Signs user session cookies and CSRF tokens. | AWS Secrets Manager / Environment variable (`SECRET_KEY`). Minimum 32 random characters. | Rotated every 90 days or immediately upon session compromise. |
| **`AUDIT_HMAC_KEY`** | Signs HMAC-SHA256 audit log hash chains: $$\text{entry\_hash} = \text{HMAC-SHA256}(K, \text{prev\_hash} \parallel \text{seq} \parallel \text{payload})$$ | Stored in AWS Secrets Manager with separate access policy. **Never stored inside database backups.** | Rotated annually or following an incident. Requires full chain verification before rotation. |
| **`ANCHOR_SIGNING_KEY`** | Signs external anchor manifests before submission to Object Lock storage. | KMS / Hardware Security Module (HSM). | Annual rotation. |

### 3.2 Escrow and Emergency Recovery Procedure
1. **Separation from Data Backups:** Under no circumstances is the `AUDIT_HMAC_KEY` or `SECRET_KEY` included in database backups or filesystem snapshots. A compromised backup snapshot must not yield the cryptographic integrity key.
2. **Dual-Custody Key Escrow:**
   - The production `AUDIT_HMAC_KEY` is divided into two shares using Shamir's Secret Sharing (2-of-2 scheme) or encrypted with two independent public keys held by separate security custodians (Chief Information Security Officer and Head of Operations).
   - Reconstitution of the escrowed key requires simultaneous cryptographic authorization from both custodians.
3. **Escrow Verification Drill:** Key recovery from escrow is tested semi-annually in an isolated environment as part of disaster recovery readiness.

---

## 4. Incident Response Procedure for Suspected Key Compromise

If `SECRET_KEY` or `AUDIT_HMAC_KEY` is suspected of leakage, tampering, or unauthorized disclosure:

### Step 1: Immediate Containment
1. Rotate `SECRET_KEY` immediately in Secrets Manager and restart application workers. This invalidates all active user sessions instantly.
2. Freeze all non-essential administrative access and place incoming API endpoints into maintenance mode.

### Step 2: Audit Chain Cryptographic Forensics
1. Execute full audit chain verification using the current key against the last verified external anchor manifest:
   ```bash
   python scripts/verify_audit.py --chain all
   ```
2. Identify the exact sequence number and timestamp of the last valid entry. Any mismatch between chain head and external anchor indicates tampering.
3. Preserve all raw database rows, access logs, and container logs to secure evidence storage.

### Step 3: Key Rotation & Re-Anchoring
1. Generate a new cryptographically secure key (minimum 32 bytes from `/dev/urandom`):
   ```bash
   openssl rand -base64 32
   ```
2. Update Secrets Manager with the new `AUDIT_HMAC_KEY`.
3. Perform a zero-downtime rolling restart of worker pods/containers.
4. Record an immutable system audit entry:
   `AUDIT_KEY_ROTATED` with event metadata recording the key version transition and verification state.
5. Generate an immediate daily anchor manifest:
   ```bash
   python scripts/anchor_audit.py
   ```

### Step 4: Post-Mortem & Reporting
1. Produce an incident report detailing root cause, scope of compromise, and cryptographic integrity findings.
2. Submit findings to the compliance and legal review boards.

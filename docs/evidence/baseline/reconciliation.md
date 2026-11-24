# Baseline Documentation & Codebase Reconciliation

This report documents every contradiction and discrepancy identified between `goal.md`, `README.md`, `docs/*.md`, and the actual base commit codebase (`3d49effc6c1a5e3e871447ffa671ebccc08d325a`).

---

## Contradictions and Discrepancies Log

### 1. User Interface Surface (Jinja2 Templates)
- **Documented Claim**: `goal.md` Section 1.3 and `README.md` line 3 claim the platform includes "server-rendered Jinja2 templates" for customers, staff, assessors, investigators, and managers.
- **Actual Codebase**: No Jinja2 templates, HTML views, or UI static assets exist. `app/web/` contains only an empty `__init__.py`. Only FastAPI JSON REST API endpoints exist.
- **Resolution Path**: Decision D2 approved server-rendered Jinja2 UI + API. UI templates will be built in Phase P8.

### 2. Append-Only Audit Immutability
- **Documented Claim**: `README.md` line 8, `docs/ARCHITECTURE.md`, and `decisions.tsv` claim an "append-only audit log secured with HMAC-SHA256" and that "audit log cannot be modified even by application bugs".
- **Actual Codebase**: `deploy/init-db.sql` line 28 has the `REVOKE UPDATE, DELETE` statement commented out. Default privileges grant `app_rw` full `SELECT, INSERT, UPDATE, DELETE` on all tables. Alembic migrations define no BEFORE UPDATE/DELETE triggers. Both UPDATE and DELETE were executed and verified in `reproduce_f1.py`.
- **Resolution Path**: P3 will implement database-level append-only triggers, revoke app role update/delete grants, and require audit functions via `SECURITY DEFINER`.

### 3. Concurrency Safety of Audit Chains
- **Documented Claim**: `docs/ARCHITECTURE.md` asserts independent per-claim chains serialize updates safely.
- **Actual Codebase**: `write_audit_log` uses `SELECT ... FOR UPDATE` on `AuditChainHead`. When a new chain is first written, no head row exists, leading to concurrent insertion races. Reproduced in `reproduce_f2.py` (90% failure under 10 concurrent threads).
- **Resolution Path**: P3 will implement transaction-scoped advisory locks on chain keys before head inspection or function insertion.

### 4. Object-Level Access on Mutations
- **Documented Claim**: `README.md` line 9 and `goal.md` claim server-side RBAC and object-level authorization on all operations.
- **Actual Codebase**: Only 3 read endpoints in `app/api/claims.py` call `check_claim_access`. All state-changing mutation endpoints (`make_decision`, `create_assessment`, `review_document`, `transition_status`, `assign_investigator`, `start_review`, `add_finding`, `escalate_investigation`, `complete_investigation`) omit object-level authorization checks.
- **Resolution Path**: P1 will enforce service-level authorization inside every claim-scoped service method.

### 5. Risk Score and Evaluation Visibility
- **Documented Claim**: `goal.md` Section 6 requires that fraud indicator and risk data prompt human investigation only and remain internal to authorized insurance personnel.
- **Actual Codebase**: `app/api/claims.py` lines 529-536 and 559-566 include `UserRole.CUSTOMER` and `UserRole.STAFF` in `require_roles` for `/risk-evaluations` endpoints. Furthermore, `tests/integration/test_risk_service.py` lines 260-272 asserts that customers can read their own risk evaluations.
- **Resolution Path**: Decision D6 forbids customer and staff access to risk data. P1 and P6 will redact risk payloads and restrict endpoints to assessor (category only), investigator, and manager.

### 6. Coverage Limit Enforcement and Atomic Approval
- **Documented Claim**: `goal.md` Section 9.2 states that coverage limits cannot be overspent under concurrency.
- **Actual Codebase**: `decision_service.make_decision` reads coverage without row-level locking, adds `payload.approved_amount` in Python, does not verify whether `consumed_amount <= limit_amount`, and skips `CoverageLedger` insertion if no coverage row is found.
- **Resolution Path**: P2 will introduce `SELECT ... FOR UPDATE` row locks, atomic SQL expressions, coverage CHECK constraints, and fail-closed validation if coverage is missing.

### 7. Claim Limit Policy Configuration
- **Documented Claim**: `docs/RULES.md` and `ruleset_v1.yaml` specify `claim_limit_policy: "CAP_WITH_WARNING"`.
- **Actual Codebase**: Hard-coded in `app/engine/validation.py` line 34 with fallback default.
- **Resolution Path**: Decision D1 specifies that limit behavior is configured per coverage as `limit_exceeded_behavior` (CAP or REJECT) with no code default.

### 8. Engine Replay Source of Truth
- **Documented Claim**: `goal.md` Section 14 and `docs/RUNBOOK.md` claim deterministic replay from recorded runs.
- **Actual Codebase**: `scripts/replay.py` queries live tables (`customers`, `policies`, `documents`) rather than stored `inputs_snapshot` fields.
- **Resolution Path**: P5 will serialize complete input snapshots into `validation_runs`, `risk_evaluations`, and `assessments` and recompute from snapshots exclusively.

### 9. Document Review Revalidation and Sensitive Code Lists
- **Documented Claim**: `goal.md` Section 8 requires that document review updates document status and unblocks claim validation.
- **Actual Codebase**: `claim_service.review_document` does not trigger revalidation. Two contradictory sensitive document code lists exist in `claim_service.py` and `rbac.py`. File uploads read the entire byte payload before size checking.
- **Resolution Path**: P1 and P4 will unify sensitive codes, stream file uploads, and trigger transactional revalidation.

### 10. Database Test Environment and Backup Drill
- **Documented Claim**: `goal.md` Section 17.13 and `decisions.tsv` claim PostgreSQL testing and backup restore drill verification.
- **Actual Codebase**: `tests/conftest.py` hardcodes in-memory SQLite for all test executions. CI runs migrations on PostgreSQL but executes tests on SQLite. `scripts/drill_backup_restore.py` silently falls back to SQLite when PostgreSQL creation fails.
- **Resolution Path**: P7 will migrate test fixtures to real PostgreSQL 16 and enforce `--cov-fail-under` thresholds.

# Hardening Progress Log

## Phase P0: Baseline, reproduction of known defects, and traceability

### Environment Setup
- **Date**: 2026-11-20
- **Base Commit**: `3d49effc6c1a5e3e871447ffa671ebccc08d325a`
- **Branch**: `hardening/v1`
- **Tools**:
  - `git --version`: `git version 2.54.0.windows.1`
  - `python --version`: `Python 3.13.15`
  - `docker --version`: `Docker version 29.9.3, build 5457199`
  - `docker-compose --version`: `Docker Compose version v5.7.0`
  - Working directory: `D:\edge-downloades\d\anudip-project`

---

### Task Log

#### Task 1: Baseline Execution and Evidence Collection
- **Files Changed**: None (baseline run).
- **Commands Run**:
  - `ruff check --no-cache .`: Passed (0 errors). Output saved to `docs/evidence/baseline/baseline_ruff.txt`.
  - `ruff format --check --no-cache .`: Passed (110 files already formatted). Output saved to `docs/evidence/baseline/baseline_ruff.txt`.
  - `mypy --cache-dir "$TEMP/mypy_cache" app`: Passed (0 issues in 60 source files). Output saved to `docs/evidence/baseline/baseline_mypy.txt`.
  - `lint-imports --no-cache`: Passed (1 contract kept, 0 broken). Output saved to `docs/evidence/baseline/baseline_lint_imports.txt`.
  - `pytest --cov=app --cov-report=term-missing`: Passed 137 of 137 tests, 88% overall statement coverage, 95-100% engine coverage. Output saved to `docs/evidence/baseline/baseline_test_run.txt`.
  - `alembic upgrade head`: Failed due to offline PostgreSQL on localhost:5432 (`psycopg.OperationalError: connection to 127.0.0.1:5432 failed`). Output saved to `docs/evidence/baseline/baseline_migrations.txt`.
  - `docker build`, `docker-compose up`: Docker daemon offline on host (`//./pipe/docker_engine` not found). Output saved to `docs/evidence/baseline/baseline_docker.txt`.
- **Status**: Completed.

#### Task 2: Reproduce Known Defects F1 to F13
- **Files Changed / Created**:
  - `docs/evidence/baseline/reproduce_f1.py`
  - `docs/evidence/baseline/reproduce_f2.py`
  - `docs/evidence/baseline/reproduce_f1_to_f13.md`
- **Execution Results**:
  - F1: Reproduced via `reproduce_f1.py` (in-place row mutation and deletion succeeded without error).
  - F2: Reproduced via `reproduce_f2.py` (10 concurrent first-writes to new chain caused 9 collisions/errors).
  - F3 - F13: Verified and recorded with exact line citations in `docs/evidence/baseline/reproduce_f1_to_f13.md`.
- **Status**: Completed.

#### Task 3: Route Authorization and Object-Level Scoping Matrix
- **Files Created**: `docs/ROUTE_MATRIX.md`
- **Details**: Full enumeration of all 61 HTTP routes with allowed roles, object-level rules, data returned, and vulnerabilities noted.
- **Status**: Completed.

#### Task 4: Requirements Traceability Matrix
- **Files Created**: `docs/TRACEABILITY.md`
- **Details**: Mapped all functional and non-functional requirements to source code, tests, and mechanical enforcement levels (`MECHANICALLY ENFORCED`, `CONVENTION ONLY`, `NOT IMPLEMENTED`, `UNVERIFIED`).
- **Status**: Completed.

#### Task 5: Decision Register Initialization
- **Files Created**: `docs/DECISIONS.md`, `docs/PROGRESS.md`
- **Details**: Populated Decision Register D1 through D14 verbatim from Section 3 of `HARDENING_PROMPT.md`.
- **Status**: Completed.

#### Task 6: Documentation and Code Reconciliation Audit
- **Files Created**: `docs/evidence/baseline/reconciliation.md`
- **Details**: Audited `goal.md`, `README.md`, `docs/*.md` against current codebase; documented all 10 contradictions and discrepancies.
- **Status**: Completed.

---

### Phase P0 Gate Summary
- **Baseline Test Outputs**: Saved in `docs/evidence/baseline/` (`baseline_test_run.txt`, `baseline_ruff.txt`, `baseline_mypy.txt`, `baseline_lint_imports.txt`, `baseline_docker.txt`, `baseline_migrations.txt`).
- **Defects F1 to F13**: Verified and recorded in `docs/evidence/baseline/reproduce_f1_to_f13.md`; F1 and F2 executed and reproduced with scripts `reproduce_f1.py` and `reproduce_f2.py`.
- **Route Matrix**: Saved in `docs/ROUTE_MATRIX.md` (61 routes enumerated).
- **Traceability Matrix**: Saved in `docs/TRACEABILITY.md` (21 requirements categorized by mechanical enforcement level).
- **Decision Register**: Saved in `docs/DECISIONS.md` (Decisions D1 to D14 initialized).
- **Documentation Reconciliation**: Saved in `docs/evidence/baseline/reconciliation.md` (10 major contradictions cataloged).
- **Status**: Ready for Owner Review. STOPPED at Phase P0 Gate.

---

## Phase P1: Security and authorization

### Task Log

#### Task 1: Service-Level Authorization (Fix F3)
- **Files Changed**:
  - `app/services/claim_service.py`
  - `app/services/decision_service.py`
  - `app/services/assessment_service.py`
  - `app/services/investigation_service.py`
  - `app/services/risk_service.py`
  - `app/services/validation_service.py`
  - `app/security/rbac.py`
- **Details**: Enforced `check_claim_access(actor, claim, action=...)` inside every public mutating and reading service method touching a claim. Customer direct mutations blocked. Admin blocked from claim decisions.
- **Verification**: Created `tests/security/test_service_authorization.py` using AST inspection of all public methods in `app/services/*_service.py` and functional tests for unauthorized customer and staff mutations. (Passed 3/3).

#### Task 2: Role Matrix and Status Masking (Per D6, D7)
- **Files Changed**:
  - `app/api/claims.py`
  - `app/schemas/claims.py`
  - `app/domain/constants.py`
- **Details**:
  - Customer: Own records only, never risk data. Status masking maps `UNDER_INVESTIGATION` and `ESCALATED` to generic `"UNDER_REVIEW"`, and status history scrubbed of investigation reasons.
  - Staff: Summary of all claims visible on list; full detail only for claims they registered.
  - Assessor: Claims in assessment; category and completeness only, indicator evidence stripped.
  - Investigator: Assigned investigations only; full risk evidence.
  - Manager: Full decision authority and oversight.
  - Admin: System management; forbidden from deciding claims; risk data restricted.
- **Verification**: `tests/security/test_p1_security.py::test_customer_status_masking_hides_investigation` (Passed).

#### Task 3: Risk Visibility (Fix F4)
- **Files Changed**:
  - `app/api/claims.py`
  - `app/services/risk_service.py`
  - `app/security/rbac.py`
- **Details**: Customer and Staff receive HTTP 403 Forbidden on all risk evaluation endpoints. Assessor receives filtered payload (scores and indicator details redacted).
- **Verification**: `tests/security/test_p1_security.py::test_risk_endpoints_role_enforcement_and_assessor_masking`, `tests/integration/test_risk_service.py` (Passed).

#### Task 4: Sensitive Document Access (Fix F8)
- **Files Changed**:
  - `app/domain/constants.py`
  - `app/security/rbac.py`
  - `app/services/claim_service.py`
- **Details**: Unified `CANONICAL_SENSITIVE_DOC_CODES` as single source of truth across access control, metadata redaction, and download. Sensitive content restricted per D8 (Assessor, Manager, assigned Investigator).

#### Task 5: Access Log Table and Fail-Closed Logging
- **Files Created / Changed**:
  - `app/db/models.py` (`AccessLog` model)
  - `alembic/versions/0002_access_log.py` (migration)
  - `app/security/access_log.py`
- **Details**: Created append-only `access_logs` table recording sensitive document views/downloads and non-owner claim views. Enforced fail-closed write-before-stream with bounded single retry.
- **Verification**: `tests/security/test_p1_security.py::test_access_log_fail_closed_and_recording` (Passed).

#### Task 6: Upload Hardening
- **Files Changed**:
  - `app/services/document_storage.py`
  - `app/api/claims.py`
- **Details**: Enforced size limits during streaming read using `Content-Length` header check and cumulative buffer threshold before buffering. Magic byte signature verification added for PDF (`%PDF`), JPEG, and PNG. Sanitized filenames using `os.path.basename`.
- **Verification**: `tests/security/test_p1_security.py::test_upload_magic_signature_validation`, `tests/integration/test_document_lifecycle.py::test_document_upload_rejects_oversized_files` (Passed).

#### Task 7: Session Security and CSRF
- **Files Changed**:
  - `app/api/auth.py`
  - `app/main.py`
  - `app/security/rate_limit.py`
- **Details**: Added session rotation on login. Transactional database-backed rate limiting. Added CSRF middleware validating `X-CSRF-Token` on state-changing requests authenticated via session cookie. Security headers enforced.
- **Verification**: `tests/security/test_auth.py` (5/5 passed).

#### Task 8: Separation of Duties
- **Files Changed**:
  - `app/services/claim_service.py`
  - `app/services/decision_service.py`
  - `app/security/rbac.py`
- **Details**: Document reviewer cannot be the uploader. Assessor who assessed claim cannot decide it. Investigator assigned cannot decide it.
- **Verification**: `tests/security/test_p1_security.py::test_separation_of_duties_document_reviewer_cannot_be_uploader`, `tests/integration/test_decision_lifecycle.py` (Passed).

---

### Phase P1 Gate Summary
- **Test Suite**: 145 passed in 44.92s, 87% coverage.
- **Ruff Lint**: Passed (`All checks passed!`).
- **Ruff Format**: Passed (122 files already formatted).
- **Mypy**: Passed (`Success: no issues found in 61 source files`).
- **Import Linter**: Passed (`Engine determinism: 1 kept, 0 broken`).
- **Status**: Complete (commit `4608d81`).

---

## Phase P2: Transactional approval and coverage accounting

### Task Log

#### Task 1: Lock Order and Retry Mechanism
- **Files Created / Changed**:
  - `app/db/lock_utils.py`
  - `app/config.py`
- **Details**:
  - Enforced strict lock hierarchy: Claim row (`FOR UPDATE`), Coverage row sorted by ID (`FOR UPDATE`), Assessment row (`FOR UPDATE`), followed by audit chain writes.
  - Added `set_lock_timeout` setting PostgreSQL transaction-scoped lock timeout (5s) via `LOCK_TIMEOUT_SECONDS`.
  - Added `@with_concurrency_retry` decorator retrying on deadlock (`40P01`) or serialization failure (`40001`).

#### Task 2: Claim Row Lock and Optimistic Version Concurrency
- **Files Changed**:
  - `app/services/claim_service.py`
  - `app/services/decision_service.py`
  - `app/services/assessment_service.py`
  - `app/schemas/decisions.py`
  - `app/schemas/claims.py`
- **Details**:
  - Injected `with_for_update()` on Claim queries across claim workflow mutations.
  - Added `expected_status_version` to `DecisionCreate` and `ClaimReopenRequest`. Stale client attempts raise `StaleStatusVersionError` mapping to HTTP 409 Conflict.

#### Task 3: Atomic Approval Transaction (Fix F5)
- **Files Changed**:
  - `app/services/decision_service.py`
  - `app/engine/assessment_cap.py`
- **Details**:
  - Decision approval locks Claim and Coverage rows; verifies Coverage exists (refuses decision if missing, never skipping ledger).
  - Recomputes remaining capacity from authoritative state by `limit_scope` (`PER_CLAIM` uses limit, `PER_POLICY_PERIOD` uses limit minus consumed).
  - Recomputes payable P' from stored assessed loss using deterministic engine `calculate_assessment_cap`.
  - Enforces `0 < approved_amount <= P'`. Rejects approvals exceeding capacity.
  - Atomically increments `Coverage.consumed_amount` using SQL expression `update(Coverage).values(consumed_amount=Coverage.consumed_amount + payload.approved_amount)` under lock.
  - Inserts `CONSUME` row in `CoverageLedger` linking `decision_id` and `claim_id`.
  - Atomically persists Decision, status transition, ClaimStatusHistory, and audit log.

#### Task 4: Database-Level Protection and Constraints
- **Files Created / Changed**:
  - `app/db/models.py`
  - `alembic/versions/0003_coverage_transactional_accounting.py`
- **Details**:
  - Added check constraint `chk_coverage_consumed_limit` on `coverages`: `limit_scope != 'PER_POLICY_PERIOD' OR consumed_amount <= limit_amount`.
  - Added check constraint `chk_coverage_limit_exceeded_behavior`: `limit_exceeded_behavior IN ('CAP', 'REJECT')`.
  - Added `limit_applied` numeric column with check constraint `chk_decision_limit_applied` on `decisions`: `limit_applied IS NULL OR approved_amount <= limit_applied`.
  - Added `decision_id` and `parent_ledger_id` foreign keys with unique constraint `uq_coverage_ledger_decision_entry` on `(decision_id, entry_type)` in `coverage_ledger`.
  - Added `is_superseded` boolean flag on `assessments`.

#### Task 5: Coverage Ledger Reconciliation
- **Files Created**:
  - `scripts/reconcile_ledger.py`
- **Details**:
  - Reconciles `consumed_amount` against sum of `CONSUME` minus `REVERSE` in `coverage_ledger`.
  - CLI and programmatic invocation (`reconcile_coverages`). Safe to rerun; never auto-fixes; creates incident record on discrepancy.

#### Task 6: Decision D1 Policy Implementation (Fix F6)
- **Files Changed**:
  - `app/engine/validation.py`
  - `app/schemas/master_data.py`
  - `app/config.py`
- **Details**:
  - Removed hardcoded `CAP_WITH_WARNING` default. `limit_exceeded_behavior` is a required NOT NULL field (`"CAP"` or `"REJECT"`) on `CoverageCreate` without default.
  - Missing or unknown coverage terms fail validation Rule V11 with `POLICY_TERMS_INCOMPLETE`.
  - Under `CAP`, V11 fails only when remaining limit is zero (`LIMIT_EXHAUSTED`); requests above limit emit `limit_warning=True`. Payable is capped with reason code `LIMIT_APPLIED`.
  - Under `REJECT`, V11 fails with `LIMIT_EXCEEDED` if requested > remaining limit.
  - Added `REQUIRE_MANAGER_WHEN_CAPPED` setting (default False); if True and limit was capped, non-manager actors are rejected.

#### Task 7: Manager Reopen and Ledger Reversal Lifecycle (Per D1)
- **Files Changed**:
  - `app/api/claims.py`
  - `app/services/decision_service.py`
  - `app/domain/state_machine.py`
- **Details**:
  - Added `POST /api/claims/{claim_id}/reopen` route for Manager role only.
  - Allowed state machine transitions: `APPROVED -> READY_FOR_ASSESSMENT` and `REJECTED -> READY_FOR_ASSESSMENT`. `WITHDRAWN` remains strictly terminal.
  - Requires mandatory remarks (min 20 characters).
  - Creates compensating `REVERSE` ledger row referencing the original `CONSUME` row (`parent_ledger_id`).
  - Decrements `Coverage.consumed_amount` via atomic SQL expression under lock.
  - Marks prior active assessments `is_superseded = True`.
  - Original decisions and ledger entries remain append-only and immutable.

#### Task 8: Verification Suites
- **Files Created**:
  - `tests/integration/test_p2_concurrency_and_ledger.py` (9 tests)
- **Details**:
  - Verified approval without coverage row fails with `DecisionValidationError`.
  - Verified approval above remaining capacity is refused.
  - Verified stale `status_version` returns HTTP 409 Conflict.
  - Verified audit write failure rolls back status, decision, ledger, and consumed amount (atomicity verified).
  - Verified capped approval records `LIMIT_APPLIED` and enforces manager threshold.
  - Verified manager reopen reverses ledger and supersedes assessment.
  - Verified reconciliation script detects clean ledger and flags incident on mismatch.
  - Verified 2 concurrent approvals over remaining limit: exactly 1 succeeds, total never exceeds limit.
  - Verified 50 concurrent approvals against limit: exactly 25 succeed (50k limit), 25 refused, consumed equals ledger sum.

---

### Phase P2 Gate Summary
- **Test Suite**: 154 passed in 50.41s, 87% coverage.
- **Ruff Lint**: Passed (`All checks passed!`).
- **Ruff Format**: Passed (126 files already formatted).
- **Mypy**: Passed (`Success: no issues found in 62 source files`).
- **Import Linter**: Passed (`Engine determinism: 1 kept, 0 broken`).
- **Route Matrix**: Updated in `docs/ROUTE_MATRIX.md` with reopen route and F5 updates.
- **Traceability Matrix**: Updated in `docs/TRACEABILITY.md` with R-08, R-09, R-22 mechanical enforcement details.
- **Status**: Complete (commit `293122b`).

---

## Phase P3: Audit integrity

### Task Log

#### Task 1: Database-Level Append-Only (Fix F1)
- **Files Created / Changed**:
  - `alembic/versions/0004_audit_integrity_append_only.py`
  - `deploy/init-db.sql`
- **Details**:
  - Revoked `UPDATE`, `DELETE`, and `TRUNCATE` permissions on all protected immutable tables (`audit_log`, `claim_status_history`, `decisions`, `investigation_findings`, `coverage_ledger`, `access_log`) from role `app_rw`.
  - Added defense-in-depth database triggers (`trg_prevent_update_delete_truncate` on PostgreSQL and `trg_no_update_*`/`trg_no_delete_*` on SQLite) that raise exceptions immediately on any attempted mutation or deletion.
  - Aligned bootstrap `init-db.sql` and Alembic migration `0004`.
- **Verification**: `tests/integration/test_p3_audit_integrity.py::test_database_level_append_only_triggers_reject_mutations` (Passed).

#### Task 2: Concurrency-Safe Appends and Serialization (Fix F2)
- **Files Changed**:
  - `app/audit/writer.py`
  - `alembic/versions/0004_audit_integrity_append_only.py`
- **Details**:
  - Eliminated the first-write race condition by querying head state authoritatively from `AuditLog` under advisory transaction locks (`pg_advisory_xact_lock` on PostgreSQL and per-chain process locks on SQLite).
  - Enforced mechanical database constraints: `UNIQUE (chain_key, seq)` (`uq_audit_chain_seq`), `UNIQUE (chain_key, prev_hash)` (`uq_audit_chain_prev_hash`), and composite self-referential foreign key `fk_audit_log_prev` (`(chain_key, prev_seq, prev_hash) REFERENCES audit_log(chain_key, seq, hash)`).
  - Maintained `AuditChainHead` as an accelerated cache updated atomically in the same transaction.
- **Verification**:
  - `tests/integration/test_p3_audit_integrity.py::test_concurrency_50_writers_on_single_chain_never_gaps_or_forks` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_concurrency_50_first_writes_to_new_chains_no_first_write_race` (Passed).

#### Task 3: Strict Schema Allowlisting and PII / Float Rejection (Fix F9)
- **Files Created / Changed**:
  - `app/audit/schema.py`
  - `app/audit/writer.py`
  - `app/api/auth.py`
  - `app/api/customers.py`
  - `app/services/claim_service.py`
  - `app/services/assessment_service.py`
  - `app/services/decision_service.py`
  - `app/services/investigation_service.py`
  - `app/services/risk_service.py`
  - `app/services/validation_service.py`
- **Details**:
  - Replaced denylist approach with per-action allowed keys schema in `ACTION_SCHEMAS`.
  - Enforced strict rejection of unknown actions, unknown keys, free-text fields (e.g. raw remarks, descriptions, findings), IEEE-754 floating-point numbers, and sensitive personal identifiers (PII).
  - Free text is converted to deterministic SHA-256 digests (`hash_free_text`) before logging.
  - Values restricted to IDs, enums, dates, fixed-scale decimal strings, and SHA-256 hashes.
- **Verification**:
  - `tests/integration/test_p3_audit_integrity.py::test_strict_allowlist_rejects_unknown_action` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_strict_allowlist_rejects_unknown_keys` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_strict_allowlist_rejects_free_text` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_strict_allowlist_rejects_long_unhashed_strings` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_strict_allowlist_rejects_pii` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_strict_allowlist_rejects_floats` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_canary_test_proves_zero_pii_leaks` (Passed).

#### Task 4: Entity Chains Architecture
- **Files Changed**:
  - `app/audit/writer.py`
  - `app/api/auth.py`
  - `app/api/customers.py`
- **Details**:
  - Segregated independent hash chains: `claim:<claim-id>`, `customer:<customer-id>`, `policy:<policy-id>`, `user:<user-id>`.
  - System chain (`system`) strictly reserved for system-level operational events (ruleset activation, key rotation, backup drills, retention reports, verification runs). Claim-path operations forbidden from writing to `system`.

#### Task 5: Length-Prefixed HMAC-SHA256 Framing
- **Files Created / Changed**:
  - `app/audit/writer.py`
  - `app/audit/schema.py`
- **Details**:
  - Secret key loaded outside database from environment (`settings.AUDIT_HMAC_KEY`).
  - Genesis computed deterministically: `sha256("genesis:" + chain_key)`.
  - Implemented length-prefixed framing over explicit fields (`<len>:<val>`) per Appendix B, incorporating `hash_version=1` and `key_id=1`.
  - Stored canonical payload text (`payload_canonical`) as immutable source of truth.

#### Task 6: External Anchoring and Access Log Integrity
- **Files Created / Changed**:
  - `app/audit/anchor.py`
  - `scripts/anchor_audit.py`
  - `app/security/access_log.py`
  - `app/db/models.py`
- **Details**:
  - Implemented daily anchor manifest generator in `app/audit/anchor.py` and CLI in `scripts/anchor_audit.py`.
  - Manifest records previous manifest root hash, chained heads, and digest of access log `row_hash` values.
  - Added per-row HMAC (`row_hash`) to `access_log` for contention-free verification.
- **Verification**: `tests/integration/test_p3_audit_integrity.py::test_access_log_integrity_and_daily_anchor` (Passed).

#### Task 7: Comprehensive Verification and Tamper Detection
- **Files Created / Changed**:
  - `app/audit/verifier.py`
  - `scripts/verify_audit.py`
  - `tests/integration/test_p3_audit_integrity.py`
- **Details**:
  - Verifier validates cryptographic linkage, sequence linearity, head-versus-last-row consistency, external anchor manifests, and access log digests.
  - Automatically identifies first broken sequence; blocks write operations on broken chains until acknowledged.
- **Verification**:
  - `tests/integration/test_p3_audit_integrity.py::test_tamper_matrix_row_mutation` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_tamper_matrix_sequence_gap` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_tamper_matrix_tail_truncation_detected_via_external_anchor` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_tamper_matrix_anchor_mismatch_detected` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_audit_failure_rolls_back_entire_business_transaction` (Passed).
  - `tests/integration/test_p3_audit_integrity.py::test_audit_log_columns_strictly_classified` (Passed).

---

### Phase P3 Gate Summary
- **Test Suite**: 171 passed in 41.05s, 85% coverage.
- **P3 Verification Suite**: 17 passed in 1.71s (`tests/integration/test_p3_audit_integrity.py`).
- **Ruff Lint**: Passed (`All checks passed!`).
- **Ruff Format**: Passed (131 files already formatted).
- **Mypy**: Passed (`Success: no issues found in 64 source files`).
- **Import Linter**: Passed (`Engine determinism: 1 kept, 0 broken`).
- **Alembic Migrations**: Passed (`test_alembic_migrations_upgrade_and_downgrade` passed upgrade/downgrade/upgrade cycle).
- **Traceability Matrix**: Updated in `docs/TRACEABILITY.md` with R-10, R-11, R-18 mechanical enforcement details.
- **Status**: Ready for Owner Review. STOPPED at Phase P3 Gate (commit `f8d453b`).

---

## Phase P4: Documents and claim lifecycle

### Task Log

#### Task 1: Document Lifecycle and Identity Attestation Schema (Fix F8, Decision D4)
- **Files Created / Changed**:
  - `alembic/versions/0005_document_lifecycle_and_identity_attestation.py`
  - `app/db/models.py`
  - `app/db/__init__.py`
- **Details**:
  - Added `Customer.identity_version` (integer version counter starting at 1, incremented on identity profile updates).
  - Added `RequiredDocumentType.is_identity_bearing` boolean flag.
  - Added unique constraint `(id, sha256)` on `claim_documents` to support immutable composite foreign keys.
  - Added DB check constraint `chk_claim_document_invalid_reason_remarks` requiring `reason_code` and `review_remarks` when `status = 'INVALID'`.
  - Added `document_identity_attestations` append-only revision table keyed `(document_id, rev)` with:
    - Composite self-referencing FK `(document_id, prev_rev)` -> `(document_id, rev)` to guarantee sequential revision trees.
    - Composite FK `(document_id, document_sha256)` -> `(claim_documents.id, sha256)` binding attestation immutably to exact file content.
    - Check constraint `chk_doc_attestation_rev_seq`: `(rev = 1 AND prev_rev IS NULL) OR (rev > 1 AND prev_rev IS NOT NULL AND prev_rev = rev - 1)`.
    - Check constraint `chk_doc_attestation_reason_remarks`: `result = 'CONFIRMED' OR (result IN ('MISMATCH', 'UNCERTAIN') AND reason_code IS NOT NULL AND remarks IS NOT NULL)`.
    - Reviewer snapshot: `reviewer_id`, `reviewer_role`, `subject_identity_version`.

#### Task 2: Domain Enums and Complete State Machine (Decision D4, D8)
- **Files Changed**:
  - `app/domain/enums.py`
  - `app/domain/state_machine.py`
  - `tests/unit/test_state_machine.py`
- **Details**:
  - Added `ClaimStatus.RISK_DATA_PENDING` and `AttestationResult` enum (`CONFIRMED`, `MISMATCH`, `UNCERTAIN`).
  - Added missing state edges in `ALLOWED_TRANSITIONS`:
    - `DOCUMENTS_PENDING -> REJECTED` (human decision with mandatory remarks and reason code).
    - `READY_FOR_ASSESSMENT -> REJECTED` and `READY_FOR_ASSESSMENT -> INFO_REQUESTED`.
    - `INFO_REQUESTED -> REGISTERED` (claimant re-evaluation loop).
    - `RISK_DATA_PENDING` transitions to `VALIDATED`, `READY_FOR_ASSESSMENT`, `REJECTED`, `WITHDRAWN`.
- **Verification**: `tests/unit/test_state_machine.py::test_exhaustive_state_transitions` verified all pairs of states (Passed 2/2).

#### Task 3: Pure Engine Verification Rules V13 and R08 (Decision D4)
- **Files Changed**:
  - `app/engine/types.py`
  - `app/engine/validation.py`
  - `app/engine/risk.py`
  - `tests/engine/test_validation_engine.py`
  - `tests/engine/test_risk_engine.py`
- **Details**:
  - Rule V13: Requires status `VALID` for non-identity documents. For identity-bearing documents, requires `VALID` + current non-stale `CONFIRMED` attestation. Emits typed reason codes `DOCUMENT_MISSING`, `DOCUMENT_PENDING_REVIEW`, and `DOCUMENT_INVALID`. If only V13 fails, returns `BLOCKED_DOCS`.
  - Rule R08: Reads human identity attestation results instead of raw name comparison. Only `VALID` document metadata is trusted. Stale or unreviewed document metadata cannot trigger R08.

#### Task 4: Transactional Revalidation (Fix F8)
- **Files Changed**:
  - `app/services/claim_service.py`
  - `app/services/validation_service.py`
- **Details**:
  - Upload, replacement, review, and attestation trigger `ValidationService.validate_claim` in the same transaction with row-level claim locking.
  - Automatically transitions claims between `DOCUMENTS_PENDING`, `REGISTERED`, `VALIDATION_FAILED`, and `VALIDATED`.

#### Task 5: Decision D4 Identity Attestation Service and Endpoints
- **Files Changed**:
  - `app/schemas/documents.py`
  - `app/schemas/__init__.py`
  - `app/audit/schema.py`
  - `app/api/claims.py`
  - `app/services/claim_service.py`
- **Details**:
  - Added `IdentityAttestationCreate` and `IdentityAttestationResponse` Pydantic schemas.
  - Exposed `POST /api/claims/{claim_id}/documents/{document_id}/attestations` and `GET /api/claims/{claim_id}/documents/{document_id}/attestations`.
  - Enforced separation of duties: uploader cannot review or attest their own uploaded document.
  - Enforced role matrix: Assessor and Manager only (D8: customer and staff return 403).
  - Enforced optimistic concurrency: `expected_rev` check returns HTTP 409 Conflict on lost races.
  - Document replacement creates new row, marks prior row `SUPERSEDED`, and audits `DOCUMENT_SUPERSEDED`. Prior attestations never carry over.

#### Task 6: Investigation Chaining and Stale Claims Reporting
- **Files Created / Changed**:
  - `app/services/risk_service.py`
  - `scripts/stale_claims_report.py`
- **Details**:
  - `RiskService.evaluate_claim`: When re-evaluating in `INFO_REQUESTED` requiring investigation, sets `previous_investigation_id = prior_inv.id` linking to previous investigation.
  - `scripts/stale_claims_report.py`: Strictly read-only CLI and helper `get_stale_claims` listing non-final claims aging >= 30 days without mutating database state.

#### Task 7: Comprehensive Phase P4 Integration Verification Suite
- **Files Created**: `tests/integration/test_p4_document_lifecycle.py`
- **Execution Results**:
  - `test_document_replacement_and_superseding_history`: Passed.
  - `test_identity_attestation_revision_linearity_and_concurrency`: Passed.
  - `test_db_level_attestation_sequence_check_constraint`: Passed.
  - `test_identity_attestation_stale_detection_on_customer_version_increment`: Passed.
  - `test_separation_of_duties_uploader_cannot_review_or_attest`: Passed.
  - `test_role_matrix_for_document_review_and_attestation`: Passed.
  - `test_invalid_and_mismatch_require_reason_code_and_remarks`: Passed.
  - `test_r08_isolation_from_invalid_documents_and_stale_attestations`: Passed.
  - `test_stale_claims_report_is_strictly_read_only`: Passed.

---

### Phase P4 Gate Summary
- **Test Suite**: 180 passed in 49.35s across entire project.
- **P4 Verification Suite**: 9 passed in 2.66s (`tests/integration/test_p4_document_lifecycle.py`).
- **State Machine Suite**: 2 passed in 0.64s (`tests/unit/test_state_machine.py`).
- **Ruff Lint**: Passed (`All checks passed!`).
- **Ruff Format**: Passed (134 files already formatted).
- **Mypy**: Passed (`Success: no issues found in 64 source files`).
- **Import Linter**: Passed (`Engine determinism: 1 kept, 0 broken`).
- **Alembic Migrations**: Passed (`test_alembic_migrations_upgrade_and_downgrade` passed upgrade/downgrade/upgrade cycle on PostgreSQL container).
- **Traceability Matrix**: Updated in `docs/TRACEABILITY.md` marking R-02 as MECHANICALLY ENFORCED.
- **Status**: Ready for Owner Review. STOPPED at Phase P4 Gate.

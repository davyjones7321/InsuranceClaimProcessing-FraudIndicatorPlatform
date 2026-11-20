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
- **Status**: Ready for Owner Review. STOPPED at Phase P1 Gate.

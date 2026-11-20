# API Route Authorization and Object-Level Scoping Matrix

This matrix documents every HTTP endpoint in the Claim Platform v1 codebase, the server-side role requirements, object-level access rules, data returned, and security audit findings (F3, F4, F8).

| Method | Endpoint | Allowed Roles | Object-Level Authorization Rule | Data Returned | Security Audit Finding / Notes |
|---|---|---|---|---|---|
| `GET` | `/healthz` | Public (Unauthenticated) | None (process liveness probe) | `{"status": "ok", "app": str, "version": str}` | None |
| `GET` | `/readyz` | Public (Unauthenticated) | None (verifies DB ping, ruleset hash, alembic status) | `{"status": "ready", "database": str, "alembic_version": str, "env": str, ...}` | None |
| `GET` | `/metrics` | Public (Unauthenticated) | None (system operational counts) | `{"total_claims": int, "claims_by_status": dict, "audit_chains_count": int, ...}` | None |
| `POST` | `/api/auth/login` | Public (Unauthenticated) | Enforces rate limit and Argon2 password verification | `LoginResponse` (user info, CSRF token, session ID, HttpOnly cookie) | Audit event written to system chain |
| `POST` | `/api/auth/logout` | Authenticated (Any role) | Destroys current session from DB and clears session cookie | `{"message": "Logged out successfully"}` | Audit event written to system chain |
| `GET` | `/api/auth/me` | Authenticated (Any role) | Returns own user session record | `UserResponse` (`id`, `email`, `role`, `customer_id`, `is_active`) | None |
| `POST` | `/api/customers` | `ADMIN` | None (master data creation) | `CustomerResponse` (customer details) | Audit event written to system chain |
| `GET` | `/api/customers` | `STAFF`, `MANAGER`, `ADMIN` | None (search and pagination across all customers) | `list[CustomerResponse]` | Customer role forbidden |
| `GET` | `/api/customers/{customer_id}` | Authenticated (Any role) | If role is `CUSTOMER`: `current_user.customer_id == customer_id` enforced | `CustomerResponse` (customer details) | 403 returned if customer accesses another customer's ID |
| `PUT` | `/api/customers/{customer_id}` | `ADMIN` | None (master data update) | `CustomerResponse` (updated customer details) | Audit event written to system chain |
| `POST` | `/api/policies` | `ADMIN` | Verifies referenced customer exists | `PolicyResponse` (policy with coverages, exclusions, items) | Audit event written to system chain |
| `GET` | `/api/policies` | Authenticated (Any role) | If role is `CUSTOMER`: filtered strictly to `customer_id == current_user.customer_id` | `list[PolicyResponse]` | Customer query params overridden to own customer ID |
| `GET` | `/api/policies/{policy_id}` | Authenticated (Any role) | If role is `CUSTOMER`: `policy.customer_id == current_user.customer_id` enforced | `PolicyResponse` (policy details) | 403 returned if customer accesses another customer's policy |
| `PUT` | `/api/policies/{policy_id}` | `ADMIN` | None (master data update) | `PolicyResponse` (updated policy details) | Audit event written to system chain |
| `POST` | `/api/policies/{policy_id}/coverages` | `ADMIN` | None | `CoverageResponse` | None |
| `GET` | `/api/policies/{policy_id}/coverages` | Authenticated (Any role) | If role is `CUSTOMER`: verifies `policy.customer_id == current_user.customer_id` | `list[CoverageResponse]` | 403 returned if customer accesses other policy coverages |
| `GET` | `/api/policies/{policy_id}/coverages/{coverage_id}` | Authenticated (Any role) | If role is `CUSTOMER`: verifies policy ownership | `CoverageResponse` | 403 returned if customer accesses other policy coverages |
| `PUT` | `/api/policies/{policy_id}/coverages/{coverage_id}` | `ADMIN` | None | `CoverageResponse` | Blocked if active claim references coverage |
| `DELETE` | `/api/policies/{policy_id}/coverages/{coverage_id}` | `ADMIN` | None | `{"message": "Coverage deleted successfully"}` | Blocked if active claim references coverage |
| `POST` | `/api/policies/{policy_id}/exclusions` | `ADMIN` | None | `ExclusionResponse` | None |
| `GET` | `/api/policies/{policy_id}/exclusions` | Authenticated (Any role) | If role is `CUSTOMER`: verifies policy ownership | `list[ExclusionResponse]` | 403 returned if customer accesses other exclusions |
| `DELETE` | `/api/policies/{policy_id}/exclusions/{exclusion_id}` | `ADMIN` | None | `{"message": "Exclusion deleted successfully"}` | None |
| `POST` | `/api/policies/{policy_id}/insured-items` | `ADMIN` | None | `InsuredItemResponse` | None |
| `GET` | `/api/policies/{policy_id}/insured-items` | Authenticated (Any role) | If role is `CUSTOMER`: verifies policy ownership | `list[InsuredItemResponse]` | 403 returned if customer accesses other insured items |
| `DELETE` | `/api/policies/{policy_id}/insured-items/{item_id}` | `ADMIN` | None | `{"message": "Insured item deleted successfully"}` | None |
| `POST` | `/api/required-documents` | `ADMIN` | None | `RequiredDocumentTypeResponse` | None |
| `GET` | `/api/required-documents` | Authenticated (Any role) | None (system-wide document requirements catalog) | `list[RequiredDocumentTypeResponse]` | Public to authenticated users |
| `GET` | `/api/required-documents/{id}` | Authenticated (Any role) | None | `RequiredDocumentTypeResponse` | None |
| `PUT` | `/api/required-documents/{id}` | `ADMIN` | None | `RequiredDocumentTypeResponse` | None |
| `DELETE` | `/api/required-documents/{id}` | `ADMIN` | None | `{"message": "Required document type deleted successfully"}` | None |
| `POST` | `/api/import/batch` | `ADMIN` | None (transactional batch load with dry_run option) | `BatchImportResponse` (counts, rejects, errors) | Dry-run and commit modes |
| `POST` | `/api/import/csv` | `ADMIN` | None (CSV tabular import for customers and policies) | `BatchImportResponse` (counts, rejects, errors) | Multi-part file upload |
| `POST` | `/api/claims` | `CUSTOMER`, `STAFF`, `ADMIN` | If role is `CUSTOMER`: policy must belong to `current_user.customer_id` and channel is set to `CUSTOMER` | `ClaimResponse` (`claim_ref`, `status: "REGISTERED"`, etc.) | Initial status history and genesis audit chain written |
| `GET` | `/api/claims` | Authenticated (Any role) | If role is `CUSTOMER`: filtered strictly to `customer_id == current_user.customer_id` | `list[ClaimResponse]` | Staff currently sees all claims (F3/D7 violation) |
| `GET` | `/api/claims/{claim_id}` | Authenticated (Any role) | `check_claim_access`: Customer sees own; Staff/Assessor/Investigator/Manager/Admin allowed | `ClaimResponse` (full claim details) | Properly scoped read |
| `GET` | `/api/claims/{claim_id}/history` | Authenticated (Any role) | `check_claim_access` enforced | `list[ClaimStatusHistoryResponse]` | None |
| `POST` | `/api/claims/{claim_id}/transition` | `STAFF`, `ASSESSOR`, `INVESTIGATOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `ClaimResponse` | **Finding F3:** Mutation lacks object-level check |
| `POST` | `/api/claims/{claim_id}/documents` | `CUSTOMER`, `STAFF`, `ADMIN` | If role is `CUSTOMER`: policy ownership verified in service | `DocumentResponse` | **Finding F8:** Upload reads entire file before size check |
| `GET` | `/api/claims/{claim_id}/documents` | Authenticated (Any role) | `check_claim_access` enforced | `list[DocumentResponse]` | None |
| `GET` | `/api/claims/{claim_id}/documents/{document_id}` | Authenticated (Any role) | `check_document_access`: customer must own claim; sensitive doc codes restricted | Streaming file response | **Finding F8:** Two contradictory sensitive doc code lists |
| `POST` | `/api/claims/{claim_id}/documents/{document_id}/review` | `STAFF`, `ASSESSOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `DocumentResponse` | **Findings F3, F8, F9:** Lacks claim access check; review does not trigger revalidation; remarks leak into audit event data |
| `POST` | `/api/claims/{claim_id}/assessments` | `ASSESSOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `AssessmentResponse` | **Finding F3:** Mutation lacks claim access check |
| `GET` | `/api/claims/{claim_id}/assessments` | Authenticated (Any role) | None | `list[AssessmentResponse]` | Should enforce claim access |
| `GET` | `/api/claims/{claim_id}/assessments/latest` | Authenticated (Any role) | None | `AssessmentResponse` | Should enforce claim access |
| `POST` | `/api/claims/{claim_id}/validate` | `STAFF`, `ASSESSOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `ValidationRunResponse` | Runs validation engine and writes audit |
| `GET` | `/api/claims/{claim_id}/validation-runs` | Authenticated (Any role) | None | `list[ValidationRunResponse]` | Should enforce claim access |
| `GET` | `/api/claims/{claim_id}/validation-runs/latest` | Authenticated (Any role) | None | `ValidationRunResponse` | Should enforce claim access |
| `POST` | `/api/claims/{claim_id}/evaluate-risk` | `STAFF`, `ASSESSOR`, `INVESTIGATOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `RiskEvaluationResponse` | Runs risk engine and writes audit |
| `GET` | `/api/claims/{claim_id}/risk-evaluations` | `CUSTOMER`, `STAFF`, `ASSESSOR`, `INVESTIGATOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `list[RiskEvaluationResponse]` | **Finding F4:** Customers and staff can view full risk evaluations, violating Decision D6 |
| `GET` | `/api/claims/{claim_id}/risk-evaluations/latest` | `CUSTOMER`, `STAFF`, `ASSESSOR`, `INVESTIGATOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `RiskEvaluationResponse` | **Finding F4:** Customers and staff can view full risk evaluations, violating Decision D6 |
| `POST` | `/api/claims/{claim_id}/investigations/assign` | `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `InvestigationResponse` | **Finding F3:** Mutation lacks claim access check |
| `POST` | `/api/claims/{claim_id}/investigations/start` | `INVESTIGATOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `InvestigationResponse` | **Finding F3:** Mutation lacks claim access check |
| `POST` | `/api/claims/{claim_id}/investigations/findings` | `INVESTIGATOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `InvestigationFindingResponse` | **Finding F3:** Mutation lacks claim access check |
| `GET` | `/api/claims/{claim_id}/investigations/findings` | `INVESTIGATOR`, `MANAGER`, `ADMIN` | None | `list[InvestigationFindingResponse]` | Should enforce claim access |
| `POST` | `/api/claims/{claim_id}/investigations/escalate` | `INVESTIGATOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `InvestigationResponse` | **Finding F3:** Mutation lacks claim access check |
| `POST` | `/api/claims/{claim_id}/investigations/complete` | `INVESTIGATOR`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `InvestigationResponse` | **Finding F3:** Mutation lacks claim access check |
| `GET` | `/api/claims/{claim_id}/investigations` | `INVESTIGATOR`, `MANAGER`, `ADMIN` | None | `InvestigationResponse` | Should enforce claim access |
| `POST` | `/api/claims/{claim_id}/decisions` | `STAFF`, `MANAGER`, `ADMIN` | **MISSING `check_claim_access`** | `DecisionResponse` | **Findings F3, F5:** Lacks claim access check; coverage read-modify-write without row lock; skips ledger if no coverage |
| `GET` | `/api/claims/{claim_id}/decisions` | Authenticated (Any role) | None | `list[DecisionResponse]` | Should enforce claim access |
| `GET` | `/api/analytics/dashboard` | `MANAGER`, `ADMIN` | Role check enforced | `AnalyticsDashboardResponse` | Only managers and admins can view business analytics |
| `GET` | `/api/analytics/export` | `MANAGER`, `ADMIN` | Role check enforced | Streaming CSV response | Only managers and admins can export analytics |

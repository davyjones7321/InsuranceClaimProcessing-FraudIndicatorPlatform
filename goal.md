# AUTHORITATIVE SPECIFICATION AMENDMENTS — 2026-11-19

> **Precedence:** This amendment is part of the goal and supersedes any conflicting wording in the original specification below. Read it before implementation. The original specification remains applicable wherever it does not conflict with this amendment. The technical assessment was discussion only: no implementation, repository changes, or decisions are implied by it.

## A. Decisions and implementation blockers

1. **Claim-limit policy is not yet approved.** The proposed policy is to allow a claim whose requested amount exceeds the remaining coverage limit, issue a warning, and cap the eventual payable amount at the remaining limit. The alternative is to reject/fail the claim when the request exceeds the remaining limit. The owner must explicitly choose one before implementing V11, limit calculations, assessment, approval, or the related state transitions. Do not silently choose either policy.
2. The preferred design, pending that decision, is that the limit applies to the **payable amount after product-specific adjustments**, not the raw requested amount. Add `limit_scope` (`PER_CLAIM` or `PER_POLICY_PERIOD`) to coverage. Define the policy period precisely in the approved decision and rules documentation.
3. The authoritative limit check must occur transactionally at approval, not only during initial validation. Initial validation may provide a pre-check/warning, but it cannot reserve or consume limit.
4. A manager correction to an approved/rejected decision is proposed as a controlled reopen to `READY_FOR_ASSESSMENT`, with mandatory remarks and a compensating ledger reversal. This supersedes the old claim that final states can never change, but only for the manager correction path. All other actors remain unable to alter final claims. Implement only after the claim-limit decision is approved.

## B. Document lifecycle and V13

- V13 passes only if every mandatory document type has at least one document with status `VALID`.
- Distinguish `DOCUMENT_MISSING`, `DOCUMENT_PENDING_REVIEW`, and `DOCUMENT_INVALID`; all produce `BLOCKED_DOCS` when V13 is the only failing rule.
- Add `SUPERSEDED` to document status. Replacing a document creates a new row and marks the prior row `SUPERSEDED`; never overwrite or delete document history.
- Marking a document `INVALID` requires a reason code and remarks.
- Add explicit `document.review` permission for `assessor` and `staff`. A review action triggers revalidation.
- R08 may use document metadata only from `VALID` documents. Pending or invalid document metadata is not trusted evidence.

## C. Audit design and privacy

Replace the original single SHA-256 chain design with:

- Independent chains: one per claim (`chain_key = claim:<claim-id>`) and one `system` chain for non-claim events.
- `audit_chain_heads(chain_key, last_seq, last_hash)`, locked with `SELECT ... FOR UPDATE` while appending. Use `seq`, not the surrogate row ID, as chain order.
- Fixed lock order for operations that need multiple locks: coverage row, claim row, then chain-head row.
- Backstops: `UNIQUE(chain_key, seq)` and `UNIQUE(chain_key, prev_hash)`.
- HMAC-SHA256, with the key outside the database in environment/secret-manager storage. Never store the HMAC key in the database or repository.
- Genesis value: `sha256("genesis:" + chain_key)`.
- Canonical JSON: sorted keys, UTF-8, no whitespace; Decimal values as fixed-scale strings; UTC timestamps as ISO-8601 with microseconds and `Z`; lowercase UUIDs; reject floats. Hash all event fields except `hash`, including `chain_key` and `seq`.
- Daily export of chain-head hashes to storage/credentials the application cannot write. This external anchor is required to detect tail truncation.
- Verification uses a read-only DB role and checks linkage, sequence gaps, head-versus-last-row consistency, and latest external anchor. Report the first broken row. Run incremental verification nightly and a full verification weekly.
- Never repair or rewrite a broken chain. Treat a break as a security incident, record it in `verification_runs`, and resume only through a signed `CHAIN_BREAK_ACKNOWLEDGED` event.
- Concurrency test: at least 50 concurrent writers, followed by verification proving no gaps or forks.
- Audit events must never contain raw PII, medical content, document contents, or unrestricted before/after snapshots. Use an allowlist serializer; record changed field names and only approved non-PII values. Add tests that reject sensitive fields. Sensitive snapshots, where genuinely required for replay, must be separately access-controlled and governed as sensitive data.
- Keep audit append-only at the database permission level. Audit writes remain in the same transaction as the business change; audit failure rolls back the business transaction.

## D. Coverage-limit ledger and approval transaction

Subject to approval of the open policy in Section A:

- Add `coverages.limit_scope` and `coverages.consumed_amount`, with DB constraint `0 <= consumed_amount <= limit_amount`.
- Add append-only `coverage_ledger` entries for `CONSUME` and `REVERSE`. The ledger is the source of evidence; `consumed_amount` is the transactionally maintained balance/backstop.
- During approval, lock the coverage row, recompute remaining capacity, enforce the approved limit policy, insert the ledger entry, update `consumed_amount`, and write the decision and state transition in the same transaction.
- Add reconciliation tests and a scheduled reconciliation job proving `consumed_amount` equals the applicable ledger sum.
- A manager-only correction/reopen path must create a reversal ledger entry; never edit or delete the original decision or ledger row.
- Do not rely on V11 alone to protect against concurrent approvals.

## E. Assessment calculation and product profiles

- The assessor supplies `assessed_loss`, constrained to be no greater than the requested amount. The deterministic assessment engine computes `payable_amount`; the approved amount cannot exceed it.
- Store copay as `NUMERIC(5,2)` percent, constrained to 0–100.
- Parse all monetary/rate values from YAML strings into `Decimal`; reject floats. Do not round intermediate steps. Round once at the end using `ROUND_HALF_UP` to two decimal places.
- Use a product-profile registry in code: `PRODUCT_PROFILES[HEALTH|VEHICLE|PROPERTY]` defines the assessment input schema, ordered calculation steps, and extra validation rules. YAML rules declare `applies_to: [products]`. Do not build a plugin system.
- Ordered assessment steps:
  - HEALTH: deductible, copay, then limit cap.
  - VEHICLE: parts depreciation, deductible, then limit cap.
  - PROPERTY: coinsurance factor `min(1, sum_insured / value_at_risk)`, deductible, then limit cap.
- Return a step-by-step calculation trace. Test boundaries: assessed loss at/below deductible yields zero; remaining limit zero yields zero; copay 100% yields zero.
- Add `policy_insured_items` and `claims.insured_item_id`. Validate that the claimed vehicle/property matches the insured item and that the claimant is an insured member.
- Derive claim type from the policy/product profile; remove redundant `claims.claim_type` and remove V06 (claim type vs product mismatch).
- Explicit v1 exclusions: annual aggregate deductibles, third-party vehicle liability, cashless health, and multi-currency.

## F. Risk indicator governance and access

- Each risk indicator definition must include a rationale and `status: PILOT|VALIDATED`. Pilot indicators display a visible PILOT banner; do not present unvalidated thresholds as proven.
- Add `scripts/backtest.py` to report overall flag rate, per-indicator hit rate, and precision against known investigation outcomes where those outcomes exist. Do not invent labels or claim statistical validity from inadequate data.
- Define all lookback windows using the configured business date derived from `registered_at`; exclude the claim being evaluated and exclude `WITHDRAWN` claims. State the exact window boundaries in rules documentation.
- Risk engine input is a typed `RiskInput` that excludes protected attributes, including age, gender, religion, and ethnicity. Enforce with a test that the engine schema/input contains none of them.
- Risk data visibility: customers and general staff must not see risk scores or indicator details. Investigators and managers may see full evidence; assessors may see category only; admins have controlled operational access. Enforce server-side.
- Evidence collection and false-positive sign-off are client/insurer responsibilities and must be documented in the runbook.
- Indicators are prompts for human investigation only. They must never automatically reject a claim or use fraud-declaring language.

## G. State-machine and workflow corrections

- Add a reachable human decision path from `DOCUMENTS_PENDING` to `REJECTED` when required documents are not provided, with mandatory remarks and reason code. Preserve the ability to return to registration/revalidation when documents are supplied.
- Add human decision paths from `READY_FOR_ASSESSMENT` to `REJECTED` and `INFO_REQUESTED`, so a claim with completed investigation concerns need not be assessed before a human can decide to reject or request information.
- Manager correction/reopen from `APPROVED` or `REJECTED` to `READY_FOR_ASSESSMENT` is the sole exception to final-state immutability, and remains blocked pending approval of Section A. It requires remarks, an audit event, and any required coverage-ledger reversal.
- Re-evaluation after `INFO_REQUESTED` that again requires investigation opens a new investigation linked to the previous investigation; retain all prior records.
- No automatic expiry or closure of stale claims. Surface stale claims in analytics for human action.
- Every pipeline operation locks the claim row with `SELECT ... FOR UPDATE` and uses `status_version` for stale-client detection. Define and test a consistent lock order.
- Every date-based rule uses one configured business timezone. Store timestamps in UTC; derive business dates from that timezone (initial deployment default may be `Asia/Kolkata`, but make it explicit configuration). Pass evaluation/business dates into the pure engine.

## H. Roles, privacy, and data access

- Remove the broad `staff` permission to view all claims. Staff may access only claims they registered or are explicitly assigned/authorized to service; managers retain oversight. Customers see only their own records. Investigators see only assigned investigations. Enforce object-level authorization on every endpoint.
- Sensitive identity/medical documents are visible only to assessors, the assigned investigator, and managers. Staff/customer access is denied unless a specifically documented business workflow requires a narrow exception. Log every sensitive-document view/download and every non-owner sensitive-record view.
- Encrypt data in transit, at rest, and in backups. No PII in application logs; mask email/phone where operationally necessary.
- Add `scripts/retention_report.py`, report-only; it must never delete data. Document retention, lawful basis/consent, erasure procedure, and breach notification as client/insurer operational responsibilities requiring jurisdiction-specific legal review. An independent penetration test is required before go-live; it is not represented as completed by the build.
- Phase 8 adds dependency audit, SAST, ZAP baseline in staging, incident-response runbook, and upload antivirus hook (integration point; do not claim scanning is active unless configured and verified).

## I. Integration and operational requirements

- Add `source_system` and `external_ref` to customers, policies, and claims, with `UNIQUE(source_system, external_ref)` where both are populated.
- Implement a versioned CSV/JSON import service with schema validation, dry-run mode, idempotent upserts, and audit entries. Include it in Phase 2. Preserve existing `DocumentStorage` abstraction.
- Defer webhooks, outbox/event bus, identity verification, and machine-client API authentication in v1.
- Use a database-backed session store and database-backed login rate-limit/lockout state; in-memory state is not sufficient with multiple workers.
- Nightly/weekly jobs, backups, and reconciliation use the platform scheduler or cron. Do not add Celery or Redis solely for scheduling.
- v1 supports one configured currency only; make it a configuration constant and reject other currencies explicitly.

## J. Original requirements explicitly retained

The following remain unchanged: no LLM/ML or external AI calls; HEALTH, VEHICLE, and PROPERTY support; deterministic pure engines; human decisions; no automatic rejection from risk scores; server-side RBAC and object-level checks; append-only audit; mechanical enforcement for critical requirements; phase gates; evidence-based reports; and production deployment as the end goal.

## K. Required documentation and test updates

Update `docs/RULES.md`, `docs/ARCHITECTURE.md`, `docs/ASSUMPTIONS.md`, `docs/RUNBOOK.md`, schema/migrations, API permissions, and acceptance tests to match these amendments. Add golden fixtures and an `engine_version` to persisted engine results; any behavior-changing engine change requires an engine-version bump. Keep ruleset version and engine version distinct.

The final report format in the original goal remains mandatory. Every requirement must be labelled mechanically enforced or convention-only, with evidence. Report unrequested changes separately, including an explicit empty section when none were made.

---

# ORIGINAL GOAL (BASELINE)

# GOAL: Insurance Claim Processing & Fraud Indicator Platform (v1)

Build and deploy to production a deterministic, rule-driven claim processing platform with a risk indicator engine. No LLM or ML of any kind in v1.

## 0. How to work (read first)

1. Work phase by phase (Section 14). Do not start a phase until the previous phase's gate passes.
2. Do only what is asked. If you change, add, or remove anything not requested in this file (dependency, file layout, rule, threshold, schema, config), list it under **"Unrequested changes"** in your report. Do not fold it silently into the work.
3. Report with evidence, not summaries. For every phase, paste the actual output of: the test run, the lint/type-check run, and the migration run. "All tests pass" without output does not count.
4. For every requirement, say whether it is **mechanically enforced** (test, DB constraint, import rule, CI check) or **convention only**. Anything critical (determinism, audit immutability, role checks, mandatory remarks, no automatic fraud decision) must be mechanically enforced.
5. Do not narrow scope. If a requirement is stated generally, implement it generally (all three claim types, all roles, all modules).
6. If something is ambiguous, pick the safest reading, write it in `docs/ASSUMPTIONS.md`, and continue.
7. Never invent results. If a command fails, show the failure and fix it.

## 1. Business goal

Insurers lose time and money verifying claims by hand. This platform lets customers or staff register claims against active policies, automatically validates them (policy, coverage, limits, dates, documents, history), generates **risk indicators** for investigators, routes flagged claims to investigation, lets authorised humans assess and decide, and gives management analytics. Everything is audited.

Key principles:
- Risk indicators **assist investigators**. The system never declares a claim fraudulent and never auto-rejects because of a risk score.
- Every decision is made by a human user with mandatory remarks.
- The validation and risk engine is deterministic and replayable.

## 2. Non-goals for v1

- No LLM, ML, scoring models, or external AI calls. (Keep the code structured so a separate advisory module could be added later, but build nothing for it.)
- No payment/payout processing.
- No policy sales, premium billing, or renewals (policies are loaded/managed, not sold).
- No mobile app, no multi-tenancy, no multi-language UI.

## 3. Stack

- Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2
- PostgreSQL 16 (dev, test, and production all use Postgres, to avoid SQLite/Postgres drift)
- UI: server-rendered Jinja2 templates + minimal JS (HTMX allowed). No SPA framework.
- Rules: versioned YAML files loaded and validated at startup
- Tests: pytest, hypothesis (for engine properties), pytest-cov
- Quality: ruff, mypy (strict on `engine/`), import-linter
- Packaging/deploy: Docker, docker-compose, gunicorn with uvicorn workers, Caddy (or Nginx) for TLS
- CI: GitHub Actions
- Money: `Decimal` (NUMERIC(14,2) in DB). Never float.

## 4. Repository layout

```
app/
  main.py
  config.py                # env-based settings, validated at startup
  db/                      # models, session, base
  domain/                  # enums, value objects, state machine
  engine/                  # PURE deterministic logic (no DB, no I/O, no clock, no randomness)
    validation.py
    risk.py
    assessment_cap.py
    types.py               # input/output dataclasses
  rules/                   # YAML rule files, versioned
    ruleset_v1.yaml
  services/                # orchestration: load data -> call engine -> persist results
  api/                     # routers
  web/                     # templates, static
  security/                # auth, RBAC, CSRF, rate limit
  audit/                   # audit writer + verifier
  analytics/
alembic/
tests/
  unit/ engine/ integration/ e2e/ security/
scripts/                   # seed, replay, verify_audit, create_admin
deploy/                    # Dockerfile, compose files, Caddyfile, backup scripts
docs/                      # ARCHITECTURE.md, RULES.md, RUNBOOK.md, ASSUMPTIONS.md, API.md
goal.md
```

## 5. Determinism rules for `engine/` (mechanically enforced)

1. `engine/` functions are pure: same inputs, same outputs.
2. `engine/` must not import: `datetime.now`, `date.today`, `time`, `random`, `uuid`, `os`, `requests`, `sqlalchemy`, `fastapi`. Enforce with import-linter plus a test that scans the source AST for forbidden calls. Build fails on violation.
3. All dates (evaluation date, today) are passed in as arguments.
4. All money is `Decimal` with an explicit rounding mode (`ROUND_HALF_UP`, 2 places), defined in one place.
5. Iteration order is fixed (sort inputs by id/date before processing).
6. Every engine result carries: `ruleset_version`, per-rule `rule_id`, `rule_version`, `inputs_snapshot`, `outcome`, `reason_code`, `message`.
7. **Replay test:** for N seeded claims, run the engine twice, and via the `scripts/replay.py` command from stored snapshots. Results must be byte-identical.
8. Hypothesis property tests: engine never raises on valid input; score is within 0..100; same input twice gives same output; shuffling input lists does not change output.

## 6. Roles and permissions

| Role | Can do |
|---|---|
| customer | View own profile/policies/claims; register claim on own active policy; upload documents; respond to info requests |
| staff | Register claims on behalf of customers; upload documents; view all claims (no decisions) |
| assessor | View claims in assessment; record assessment (eligible amount + remarks) |
| investigator | View flagged claims assigned to them; record findings and outcome; escalate |
| manager | View everything; analytics; assign investigators; make/override decisions per Section 11; escalated cases |
| admin | Manage users, roles, customers, policies, coverage, rulesets activation; cannot decide claims |

Rules:
- RBAC is enforced **server-side on every endpoint** via a dependency, default-deny. A test enumerates all routes and fails if any route lacks an explicit permission declaration (except health/login).
- Object-level checks: customers see only their own data; investigators see only assigned investigations (managers see all).
- Separation of duties (configurable, default ON): the user who assessed a claim cannot make the final decision on it; an investigator cannot assess or decide the claim they investigated.

## 7. Data model

Use UUID primary keys, `created_at`/`updated_at` (UTC), and DB constraints (NOT NULL, CHECK, FK, UNIQUE) wherever possible.

- **users**: id, email (unique), password_hash (argon2), role, customer_id (nullable FK), is_active
- **customers**: id, customer_no (unique), full_name, dob, email, phone, address, state/region, created_at
- **policies**: id, policy_no (unique), customer_id, product_type (HEALTH | VEHICLE | PROPERTY), start_date, end_date, premium, status (ACTIVE | LAPSED | CANCELLED | EXPIRED | SUSPENDED), sum_insured
- **coverages**: id, policy_id, claim_category (code), limit_amount, deductible, copay_percent, waiting_period_days, conditions (JSON), is_excluded (bool)
- **exclusions**: id, policy_id, exclusion_code, description
- **claims**: id, claim_ref (unique, format `CLM-YYYY-NNNNNN`, generated from a DB sequence), policy_id, customer_id, claim_type, claim_category, incident_date, incident_location, incident_description, requested_amount, registered_at, registered_by (user), channel (CUSTOMER | STAFF), status, status_version (optimistic lock)
- **required_document_types**: id, claim_type, claim_category (nullable), doc_code, name, is_mandatory
- **claim_documents**: id, claim_id, doc_code, file_name, storage_key, sha256, size_bytes, mime_type, document_date, named_person (for consistency checks), status (PENDING_REVIEW | VALID | INVALID), reviewed_by, review_remarks, uploaded_by
- **validation_runs**: id, claim_id, ruleset_version, evaluated_on (date), overall_result (PASS | FAIL | BLOCKED_DOCS), created_at
- **validation_results**: id, run_id, rule_id, rule_version, outcome (PASS | FAIL | SKIPPED), reason_code, message, inputs_snapshot (JSONB)
- **risk_evaluations**: id, claim_id, ruleset_version, evaluated_on, score (0..100), category (LOW | MEDIUM | HIGH), requires_investigation (bool), inputs_snapshot (JSONB)
- **risk_indicator_results**: id, evaluation_id, indicator_id, indicator_version, triggered (bool), weight_applied, evidence (JSONB), message
- **assessments**: id, claim_id, assessor_id, max_eligible_amount (engine computed cap), eligible_amount, remarks, supporting_info, created_at (append-only; new assessment supersedes, old ones kept)
- **investigations**: id, claim_id, assigned_to, assigned_by, status (ASSIGNED | UNDER_REVIEW | COMPLETED | ESCALATED), outcome (CLEARED | CONCERNS_CONFIRMED | INCONCLUSIVE | null until completed), opened_at, closed_at
- **investigation_findings**: id, investigation_id, author_id, finding_text, created_at (append-only)
- **decisions**: id, claim_id, decided_by, decision (APPROVED | REJECTED | INFO_REQUESTED), approved_amount (nullable), remarks (NOT NULL, min length from config, default 20 chars), reason_codes (array), created_at (append-only)
- **claim_status_history**: id, claim_id, from_status, to_status, changed_by, reason, created_at
- **audit_log**: id (bigserial), occurred_at, actor_id, actor_role, action, entity_type, entity_id, claim_id (nullable), before (JSONB), after (JSONB), request_id, ip, prev_hash, hash
- **rulesets**: id, version, content_sha256, is_active, activated_by, activated_at

## 8. Claim status state machine (mechanically enforced)

```
DRAFT -> REGISTERED
REGISTERED -> DOCUMENTS_PENDING | VALIDATION_FAILED | VALIDATED
DOCUMENTS_PENDING -> REGISTERED        (after docs supplied, revalidate)
VALIDATION_FAILED -> REGISTERED        (after correction by authorised user) | REJECTED (by decision)
VALIDATED -> RISK_EVALUATED
RISK_EVALUATED -> UNDER_INVESTIGATION  (if requires_investigation)
RISK_EVALUATED -> READY_FOR_ASSESSMENT (if not flagged)
UNDER_INVESTIGATION -> INVESTIGATION_COMPLETE | ESCALATED
ESCALATED -> INVESTIGATION_COMPLETE    (manager resolves)
INVESTIGATION_COMPLETE -> READY_FOR_ASSESSMENT
READY_FOR_ASSESSMENT -> ASSESSED
ASSESSED -> APPROVED | REJECTED | INFO_REQUESTED
INFO_REQUESTED -> REGISTERED           (customer/staff responds; full revalidation + risk re-evaluation)
REGISTERED | DOCUMENTS_PENDING | INFO_REQUESTED -> WITHDRAWN
```

- Transitions are defined in one table in `domain/state_machine.py`. Any other transition raises an error. A test generates all (from, to) pairs and asserts only the allowed ones succeed.
- Every transition writes `claim_status_history` and an audit entry in the same DB transaction.
- Final states: APPROVED, REJECTED, WITHDRAWN (no further changes).

## 9. Validation engine

Input: a frozen `ValidationInput` (claim, policy, coverage rows, exclusions, claim history summary, documents, evaluation date, ruleset). Output: `ValidationOutput` with per-rule results and `overall_result`.

Rules (ids, reason codes, thresholds in `ruleset_v1.yaml`; values below are defaults):

| Rule | Check | Fail reason code |
|---|---|---|
| V01 | Policy exists and belongs to the claiming customer | `POLICY_NOT_FOUND` / `POLICY_OWNER_MISMATCH` |
| V02 | Policy status is ACTIVE on the evaluation date | `POLICY_NOT_ACTIVE` |
| V03 | Incident date within policy start..end (inclusive) | `INCIDENT_OUTSIDE_POLICY_PERIOD` |
| V04 | Incident date is not in the future and not after registration date | `INCIDENT_DATE_INVALID` |
| V05 | Claim filed within N days of incident (default 90) | `FILING_WINDOW_EXCEEDED` |
| V06 | Claim type matches policy product type | `CLAIM_TYPE_PRODUCT_MISMATCH` |
| V07 | A coverage row exists for the claim category and is not excluded | `NO_COVERAGE` |
| V08 | No policy exclusion matches the claim category/incident code | `EXCLUDED` |
| V09 | Waiting period satisfied (incident date >= start + waiting_period_days) | `WAITING_PERIOD_NOT_MET` |
| V10 | Requested amount > 0 | `AMOUNT_INVALID` |
| V11 | Requested amount <= remaining limit (limit minus APPROVED amounts for the same coverage in the policy period) | `LIMIT_EXCEEDED` |
| V12 | Required fields present (incident date, location, description, amount) | `MISSING_FIELD` |
| V13 | All mandatory documents uploaded and not INVALID | `DOCUMENTS_MISSING` / `DOCUMENT_INVALID` |

Outcome mapping:
- Only V13 failing, everything else passing: `BLOCKED_DOCS`, status DOCUMENTS_PENDING, list the exact missing/invalid documents.
- Any other failure: `FAIL`, status VALIDATION_FAILED, all failing rules recorded with specific reasons (do not stop at the first failure).
- All pass: `PASS`, status VALIDATED.

A validation failure does not auto-reject. It goes to an authorised user, who corrects the data or makes a REJECTED decision with remarks.

## 10. Risk indicator engine

Runs only after validation PASS. Output: indicator results, `score` (integer 0..100), `category`, `requires_investigation`.

Scoring: `score = min(100, sum(weight of triggered indicators))`. Weights and thresholds in the ruleset file. Categories (defaults): LOW 0-24, MEDIUM 25-49, HIGH 50-100. `requires_investigation = score >= 25` (configurable).

Indicators (defaults):

| ID | Indicator | Trigger | Weight |
|---|---|---|---|
| R01 | Repeated claims by customer | >= 3 claims in last 12 months (excluding WITHDRAWN) | 20 |
| R02 | Repeated claims on policy | >= 2 claims on same policy in last 6 months | 15 |
| R03 | High amount vs limit | requested > 80% of remaining limit | 20 |
| R04 | Unusual amount vs peers | requested > 3x median approved amount for same product + category (only if >= 10 historical approved claims; otherwise SKIPPED with reason) | 15 |
| R05 | Early claim | incident within 30 days of policy start | 25 |
| R06 | Late-in-policy claim | incident within 7 days of policy end | 10 |
| R07 | Duplicate suspicion | another claim on same policy, same incident date, amount within 5% | 30 |
| R08 | Inconsistent information | document `named_person` differs from the customer/insured name (normalised comparison), or document date after incident date for pre-incident documents, or incident location outside policy region where applicable | 20 |
| R09 | Late filing | filed more than 15 days after incident | 5 |
| R10 | Round-number amount | amount is a multiple of 10,000 and >= 50,000 | 5 |

Requirements:
- Each indicator returns `triggered`, `weight_applied`, and `evidence` (the exact numbers/ids that caused it), so an investigator sees why.
- Indicators that lack data return `SKIPPED` with a reason, never a guess.
- UI wording must say "risk indicator" / "requires investigation". Never "fraud detected", "fraudulent", or similar. A test scans templates and API messages for forbidden words.
- Risk evaluation is re-run on any material claim change (amount, dates, documents) and a new `risk_evaluations` row is stored. Old rows are kept.
- No code path allows a risk score or category to set REJECTED. Enforced by a test: decisions of REJECTED require a human `decided_by`, remarks, and at least one reason code, and the decision service never reads the score to choose an outcome.

## 11. Assessment, investigation, decision

**Assessment**
- Engine computes `max_eligible_amount = max(0, min(requested, remaining_limit) - deductible)` then applies `copay_percent` (exact formula in `RULES.md`, unit-tested with worked examples).
- The assessor enters `eligible_amount` (<= `max_eligible_amount`, enforced in service and DB-level CHECK where possible) plus remarks and supporting info. Cannot exceed the cap.

**Investigation**
- Created automatically when `requires_investigation` is true. Manager assigns an investigator.
- Statuses: ASSIGNED -> UNDER_REVIEW -> COMPLETED | ESCALATED. ESCALATED goes to a manager, who resolves it to COMPLETED.
- Investigator adds findings (append-only) and sets the outcome on completion (CLEARED, CONCERNS_CONFIRMED, INCONCLUSIVE). Completion requires at least one finding.
- The claim cannot be assessed or decided while an investigation is open.

**Decision**
- Allowed: APPROVED (with amount <= assessed eligible amount), REJECTED, INFO_REQUESTED.
- `remarks` mandatory (NOT NULL + minimum length + service validation). REJECTED requires at least one reason code from a fixed list (e.g., `POLICY_NOT_ACTIVE`, `NO_COVERAGE`, `LIMIT_EXCEEDED`, `DOCUMENTS_NOT_PROVIDED`, `INVESTIGATION_CONCERNS`, `OTHER` with remarks).
- A decision is blocked unless: validation PASS (or the decision is REJECTED on a validation failure), assessment exists (for APPROVED), and no open investigation.
- Approval of amounts above a configurable threshold requires manager role.
- Decisions are append-only. A correction is a new decision by a manager with remarks, not an edit.

## 12. Audit history (mechanically enforced)

- Every create/update/transition/decision/investigation/validation/risk run writes an `audit_log` row in the same transaction as the change. If audit write fails, the whole transaction rolls back.
- Append-only: the application DB role has INSERT and SELECT only on `audit_log`, `claim_status_history`, `decisions`, `investigation_findings` (no UPDATE/DELETE). Enforced in the migration and verified by an integration test that attempts UPDATE/DELETE and expects failure.
- Hash chain: `hash = sha256(prev_hash || canonical_json(row))`. `scripts/verify_audit.py` walks the chain and reports tampering. Run it in the nightly job and expose the result to admins.
- Each claim has a timeline view built from audit data (who, what, when, before/after).
- Viewing sensitive data by staff is also logged (claim view by non-owner).

## 13. Analytics

Manager dashboard with filters (date range, product type, claim category, status):
- Claim volume over time, by product, by status
- Average and median processing time (registered -> decision), by product and by stage
- Rejection reasons (by reason code), validation failure reasons
- Risk indicator frequency, risk category distribution, investigation rate
- Investigation outcomes, average investigation duration, open/escalated counts
- Amounts: requested vs approved, by product
- Backlog by status and age

Requirements: computed with SQL aggregates (indexed), not in Python loops; CSV export; every number on the dashboard has a test against a known seeded dataset.

## 14. Delivery phases and gates

**Phase 1: Foundation.** Repo, config, Docker compose (app + Postgres), Alembic, models, auth (login, argon2, sessions, CSRF), RBAC dependency, audit writer, CI.
Gate: CI green, migrations apply on empty DB and roll back, route-permission test passes, audit append-only test passes.

**Phase 2: Master data.** Customers, policies, coverage, exclusions, required document types (CRUD for admin, read for others), seed script.
Gate: tests for all CRUD + permissions + object-level access.

**Phase 3: Claim registration + documents.** Registration (customer and staff), unique claim ref, upload (type/size/sha256 checks), document review, state machine.
Gate: state machine exhaustive test, upload security tests.

**Phase 4: Validation engine.** Pure engine, ruleset loading and validation, service orchestration, results stored.
Gate: unit test per rule (pass, fail, boundary dates), determinism and replay tests, hypothesis tests, import-linter green.

**Phase 5: Risk indicator engine.** Indicators, scoring, categories, routing, forbidden-wording test.
Gate: unit test per indicator including boundaries and SKIPPED paths; score range property test; replay identical.

**Phase 6: Assessment, investigation, decisions.** Per Section 11, separation of duties, mandatory remarks, history.
Gate: all blocked paths tested (decision with open investigation, missing remarks, amount above cap, wrong role, same-user assessor and decider).

**Phase 7: Analytics.** Dashboard + CSV export.
Gate: numbers verified on seeded dataset; query performance checked with 100k seeded claims (report timings).

**Phase 8: Hardening + production.** Section 15 and 16.
Gate: Section 17 acceptance checklist, all items demonstrated with evidence.

## 15. Security and quality requirements

- Passwords: argon2id; min length 12; login rate limiting and lockout; session cookie `HttpOnly`, `Secure`, `SameSite=Lax`; CSRF on all state-changing forms/requests; session timeout.
- Input validation via Pydantic; parameterised queries only (no string-built SQL).
- File uploads: allowlist types (PDF, JPG, PNG), max size from config, random storage keys, sha256 stored, never served from a path derived from user input, served via authorised endpoint only.
- Security headers (CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, HSTS in prod).
- PII: no PII in logs; mask email/phone in log lines; secrets only from env or secret manager, never in repo (add secret scanning in CI).
- Dependency audit (`pip-audit`) and Docker image scan in CI; pinned dependencies with lock file.
- Optimistic locking on claims (`status_version`) to prevent two users acting on the same claim state.
- Idempotency: registration and decision endpoints must be safe against double-submit.
- Coverage: >= 90% on `engine/`, >= 80% overall. Ruff, mypy (strict on `engine/`), import-linter must pass in CI.

## 16. Production deployment

- **Environments:** local (docker compose), staging, production. Same Docker image across all; config only via environment variables.
- **Image:** multi-stage Dockerfile, non-root user, no dev dependencies, healthcheck.
- **Runtime:** gunicorn + uvicorn workers behind Caddy/Nginx with automatic TLS; request size limit; timeouts set.
- **Database:** managed PostgreSQL (or a dedicated instance) with: separate roles (`app_rw` limited rights per Section 12, `migrator` for DDL), SSL, connection pooling, indexes on all FK and filter columns (claim status, policy_id, customer_id, incident_date, created_at).
- **Migrations:** Alembic, run as a separate release step before app start; every migration reversible or explicitly documented as not; test migrate-up on a copy of production-like data.
- **Storage:** documents on a persistent volume or S3-compatible bucket behind one storage adapter interface; encrypted at rest; backed up.
- **Backups:** automated daily DB backup with retention (default 30 days) plus point-in-time recovery if available; **restore tested** and documented in `RUNBOOK.md` (include the evidence of one restore drill).
- **Observability:** structured JSON logs with request id; `/healthz` (process) and `/readyz` (DB reachable, ruleset loaded, migrations current); basic metrics (request rate, latency, errors, claim counts by status); error tracking hook; alert on 5xx rate, DB down, failed audit verification.
- **CI/CD (GitHub Actions):** on PR: lint, type-check, import-linter, tests with Postgres service, security scans. On merge to main: build image, push, deploy to staging, run smoke tests; production deploy by manual approval with rollback to the previous image tag.
- **Config validation:** app refuses to start if required env vars are missing, `DEBUG` is on in production, or the active ruleset fails schema validation or hash check.
- **Ruleset governance:** changing a ruleset = new version file + admin activation (audited). Old claims keep the ruleset version they were evaluated with. Never edit an active ruleset file in place (CI check compares hash against the `rulesets` table).
- **Admin bootstrap:** `scripts/create_admin.py` creates the first admin; no default credentials anywhere.
- **Docs:** `RUNBOOK.md` covers deploy, rollback, backup/restore, rotating secrets, adding a user, activating a ruleset, verifying the audit chain, and common failures.

## 17. Acceptance checklist (all must be demonstrated with evidence)

1. A customer registers a claim on their own ACTIVE policy and gets a unique claim reference and status REGISTERED.
2. A claim with missing mandatory documents stops at DOCUMENTS_PENDING and lists the exact missing documents; supplying them lets it proceed.
3. Each validation rule (V01-V13) has a failing example that records the specific reason code and message.
4. A clean claim moves through validation, risk evaluation (LOW), assessment, and decision with no investigation.
5. A claim that triggers indicators (score >= 25) is routed to investigation, cannot be assessed or decided until the investigation completes, and the UI shows the indicators with their evidence and never uses fraud-declaring wording.
6. No code path converts a risk score into a rejection (test present and passing).
7. Decisions require remarks; REJECTED requires reason codes; separation of duties is enforced.
8. Approved amount can never exceed the assessed eligible amount, and the eligible amount can never exceed the engine cap.
9. Audit log is append-only at DB level; hash chain verification passes and fails correctly on a deliberately tampered copy.
10. Engine determinism: replay script reproduces stored validation and risk results exactly for all seeded claims.
11. Every route has an explicit permission; a customer cannot read another customer's claim (test).
12. Analytics numbers match the seeded dataset.
13. Fresh deploy from scratch to staging works using only `RUNBOOK.md`; production deploy done; rollback tested; backup restored once successfully.
14. CI is green on the final commit with the coverage thresholds met.

## 18. Seed data (for demo and tests)

Provide `scripts/seed.py` that creates: users for each role, 20+ customers, policies across HEALTH, VEHICLE, PROPERTY (including LAPSED, CANCELLED, EXPIRED ones), coverage rows with limits/deductibles/waiting periods, required document types for each claim type, and 200+ historical claims across all statuses (including repeats, duplicates, early claims, round amounts) so every validation failure and every risk indicator can be demonstrated. Seeding must be deterministic (fixed random seed in the seed script only, never in `engine/`) and refuse to run when `ENV=production`.

## 19. Final report format (required from the agent)

For each phase, return:
1. What was built (short).
2. Exact test / lint / type-check / import-linter output (pasted).
3. Requirements table: requirement -> mechanically enforced or convention only -> evidence.
4. **Unrequested changes** (separate section, even if empty).
5. Open assumptions added to `docs/ASSUMPTIONS.md`.
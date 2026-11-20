# HARDENING PROMPT: Insurance Claim Processing & Fraud Indicator Platform (v1)

Task: take the EXISTING repository from "reported complete" to "verifiably correct, secure and deployable". This is not a rebuild.

Repository: https://github.com/davyjones7321/InsuranceClaimProcessing-FraudIndicatorPlatform
Base: branch `main` (audited at commit `3d49effc6c1a5e3e871447ffa671ebccc08d325a`; if `main` has moved, record the new HEAD and diff it against this commit before starting).
Working branch: create `hardening/v1` from `main`. Never work on `main`. Never merge to `main`. Never force-push. You may push `hardening/v1`.

The original requirements in `goal.md` stay in force. This file adds the corrections found by an independent audit. Where this file and `goal.md` conflict, this file wins, but only for items marked as decided in Section 3.

---

## 0. Phase overview

| Phase | Name | Depends on decisions |
|---|---|---|
| P0 | Baseline, reproduction of known defects, traceability | none |
| P1 | Security and authorization | D6, D7, D8 |
| P2 | Transactional approval and coverage accounting | D1 |
| P3 | Audit integrity | D3, D4 |
| P4 | Documents and claim lifecycle | D4, D8 |
| P5 | Assessment engine, replay, snapshots | D5, D11 |
| P6 | Risk governance | D4, D6, D9 |
| P7 | PostgreSQL tests, CI, imports, operations | D3, D10 |
| P8 | Product surface and deployment | D2, D3 |

Stop after each phase gate and wait for owner review. Do not start the next phase on your own.

---

## 1. Non-negotiable requirements

1. No LLM, ML or external AI calls anywhere in v1.
2. HEALTH, VEHICLE and PROPERTY all stay in scope. Never silently remove or narrow a category.
3. Validation, risk and assessment engines are deterministic, versioned and replayable.
4. Claim decisions are made only by authenticated humans with mandatory remarks.
5. A risk score or indicator must never reject a claim or label it fraudulent. Indicators prompt human investigation only.
6. Complete auditability. Server-side RBAC plus object-level authorization on every operation.
7. Critical requirements are mechanically enforced (database constraint, trigger, grant, import rule, or test that fails the build), not by convention.
8. Phase gates with evidence. Production deployment is the end goal.

---

## 2. Working rules

1. **Evidence only.** Never state that something passes, works or is deployed unless you ran it and can paste the actual output. Do not trust any earlier report, including `decisions.tsv` and the README.
2. **Verify every finding in Section 4 yourself** before fixing it. If a finding is wrong, say so with evidence.
3. **No silent decisions.** Business rules, thresholds, roles, retention and policy choices come from the Decision Register (Section 3). If a decision is OPEN, do not implement the dependent behaviour. Say exactly what is blocked and continue with independent work.
4. **Unrequested changes.** Anything you change that this file did not ask for goes in a separate "Unrequested changes" section of the phase report, even when it is a small refactor. If there are none, write "None".
5. **Label every requirement** in reports as: MECHANICALLY ENFORCED (name the constraint, trigger, grant or failing test), CONVENTION ONLY, NOT IMPLEMENTED, or UNVERIFIED.
6. **Real PostgreSQL for anything database-related.** SQLite results are not evidence for locking, grants, triggers, constraints or concurrency. Tests that need PostgreSQL must FAIL (not skip) when PostgreSQL is unavailable. Never use `skip`, `xfail` or weakened assertions to get green.
7. Every schema change needs an Alembic migration. Verify upgrade, downgrade and upgrade again on PostgreSQL.
8. Use `Decimal` for all money. Keep `app/engine/` pure: no database, clock, randomness, environment or network access.
9. No secrets, real customer data or credentials in the repository or logs. No PII in audit events, snapshots or logs.
10. Do not remove working functionality without a recorded reason. Prefer small, reviewable commits.
11. After every change run: focused tests, the full suite, `ruff check .`, `ruff format --check .`, `mypy app`, `lint-imports`, and the migration check. Paste the real output.
12. Before pulling or checking out anything, run `git fetch origin` (not only `git status`; status can say "up to date" while behind).
13. Keep `docs/PROGRESS.md` updated after each task: phase, task, files changed, tests added, commands run, exact results, commit SHA, remaining issues.

---

## 3. Decision Register

Create `docs/DECISIONS.md` from this table. Status values:
- **DECIDED**: the owner approved it on 2026-11-20. Implement it.
- **DECIDED (default, revisable)**: the owner delegated the choice and the best-approach default below was filled in. Implement it. The owner may change it later through a recorded decision change; handle later changes as change requests, not as defects.
- **PROVISIONAL**: approved for now; the stated condition must still be met.
- **OPEN**: not decided. Do NOT implement the dependent behaviour. Say exactly what is blocked and continue with independent work.

| ID | Decision | Approved answer | Blocks | Status |
|---|---|---|---|---|
| D1 | Claim amount above remaining coverage limit; limit basis; period; manager correction | Cap payable at the remaining limit, configured per coverage as `limit_exceeded_behavior` (CAP or REJECT) with NO code default. The limit applies to the payable amount after deductible and copay. One policy row = one coverage period. Manager-only reopen with mandatory remarks and a compensating REVERSE ledger row; original decisions and ledger rows are never edited. A `require_manager_when_capped` setting exists, default off. | V11, assessment limit step, reopen | DECIDED |
| D2 | Product surface | Server-rendered Jinja2 UI (as in goal.md), plus the existing API | P8 UI | DECIDED |
| D3 | Hosting target and audit-anchor destination | AWS, region ap-south-1 (Mumbai), so data stays in India; separate staging and production environments. Application: Docker containers via Docker Compose on an encrypted-disk EC2 instance behind Caddy for automatic TLS (single instance in v1; high availability is a later enhancement; record target recovery point and time in the runbook). Database: Amazon RDS for PostgreSQL 16, TLS enforced, encrypted at rest, not publicly reachable, automated backups with point-in-time recovery, 30-day retention. Documents: encrypted EBS volume through the existing storage adapter (an S3 adapter is deferred). Secrets: AWS Secrets Manager; the audit HMAC key is stored and backed up separately from database backups, with the escrow procedure documented. Anchors: an S3 bucket with Object Lock in compliance mode in a SEPARATE AWS account; a dedicated anchor-writer identity with PutObject only; the verifier identity read-only; the application identity has no access to that bucket. CD: GitHub Actions with a protected production environment (manual approval) and rollback to the previous image tag. | Concrete anchoring, P7 ops, P8 deployment | DECIDED (default, revisable) |
| D4 | Amended audit, document and risk design (Appendix B and every item tagged [D4]) | Approved. Implement the [D4] items. | [D4] items in P3, P4, P6 | DECIDED |
| D5 | Claim scope: one claim = one coverage category with typed assessment components ("A+"); multi-coverage claims excluded in v1 | Approved for v1, on condition that the client sample review defined in D13 is run and meets its threshold before production acceptance. If it fails, stop P5 scope work and report; do not start multi-line claims without the fallback decision in D13. | P5 acceptance | PROVISIONAL |
| D6 | Risk-data visibility | Customers and staff: never. Assessor: category and completeness only. Assigned investigator and manager: full evidence. Admin: configuration only; risk data only through audited, explicit access. | P1, P6 | DECIDED |
| D7 | Staff data minimization | Staff see a summary (reference, status, dates, amount) for all claims; full detail only for claims they registered. | P1 | DECIDED |
| D8 | Who may review and attest documents | Assessor and manager. The reviewer must differ from the uploader. Not staff. | P1, P4 | DECIDED |
| D9 | Risk weights, group caps, thresholds, completeness settings | Appendix A values, labelled unvalidated pilot values. Production use with `evidence_level NONE` needs the recorded business risk acceptance described in P6. | P6 values | DECIDED (as pilot values) |
| D10 | Retention periods, legal hold, erasure policy | Tooling: report-only `retention_report.py` and a `legal_hold` flag; nothing deletes data automatically. Configurable default periods (placeholders pending legal review, never shorter than the related claim): claim records, assessments, decisions, investigations, ledger and documents 10 years after the claim reaches a final status; audit log and access log the same as the claim they belong to; import rejects 1 year; verification and job run records 2 years; login attempts 90 days; sessions 30 days after expiry; backups 30 days. Erasure: no automated erasure in v1; a documented manual procedure requiring two approvers and an audit entry, because audit events and snapshots contain no direct identifiers and anonymising a customer row does not break chains or replay. Legal and the client confirm the periods before go-live. | P7 retention values | DECIDED (default, revisable) |
| D11 | Insurer-approved calculation specifications and golden vectors per product | Interim: ship the Appendix C formulas as an `ILLUSTRATIVE_DEFAULTS` ruleset with golden vectors, flagged in every assessment output. The calculation ruleset carries `calculation_spec_status` = ILLUSTRATIVE or APPROVED; the application refuses to activate an ILLUSTRATIVE calculation ruleset in production unless the owner records an explicit risk acceptance. Staging and tests may use it. The insurer's signed specification and golden vectors replace it before go-live. | Production use of P5 formulas | DECIDED (default, revisable) |
| D12 | Incident groups in v1 | Include. A+ relies on linking claims from one incident. Use the rules in P6 task 9 (suggest, confirm, same customer, 7-day span, size cap 5, manager-only corrections). | `incident_group_id` in P5, P6 task 9 | DECIDED (default, revisable) |
| D13 | Client sample review for A+: sample, acceptance threshold per product, and the fallback for claims that do not fit | Protocol: all claims closed in the most recent 12 months; stratified random sample by product and value band with a recorded random seed (review every claim in a stratum with fewer than about 400); two independent reviewers and a third to adjudicate, with agreement rate reported; each claim classified FIT, SPLITTABLE (can be processed as several linked A+ claims with the same total payable) or NO-FIT (needs a cross-coverage calculation, shared or aggregate deductible, independent per-item decisions, interim payment or policy-level aggregate), with a reason code. Starting acceptance threshold, set per product BEFORE the review and not changed afterwards: FIT plus SPLITTABLE at least 90% by count and at least 90% by value, and NO-FIT caused by shared aggregates or cross-coverage calculation at most 10% by count. Report Wilson 95% intervals. On a calculation sub-sample, recompute settlements with the draft spec and compare with actual settlements. If a product fails, fallback order: (1) extend A+ with the single missing concept if it explains most NO-FIT; (2) build multi-line claims for that product only; (3) build multi-line claims for all products; (4) run A+ with a documented manual process for NO-FIT claims. With no history, substitute an expert review of the policy wordings using the same rubric. The platform build is not blocked by this review; production acceptance is. | D5 condition, production acceptance | DECIDED (default, revisable) |
| D14 | `history_loaded_from` date per product, and the business timezone | Timezone: `Asia/Kolkata`, immutable for v1. History: a `history_coverage` table (product, `loaded_from`, set by, evidence reference) changed only by an admin with manager approval and audited. The default `loaded_from` is the go-live business date for each product until an import declares a `history_complete_from` date; lookback windows that start before `loaded_from` make the indicator SKIPPED with `HISTORY_WINDOW_NOT_COVERED`. | P5 timezone, P6 history indicators | DECIDED (default, revisable) |

If a decision is OPEN, record the consequence in `docs/DECISIONS.md` and keep going on independent work.

---

## 4. Known defects to verify (found by an independent audit)

Reproduce each one in P0 and save the evidence under `docs/evidence/baseline/`. "Reproduced" means a command was actually run; "Read" means found by reading code.

| # | Finding | Evidence type |
|---|---|---|
| F1 | The application DB role can `UPDATE` and `DELETE` rows in `audit_log` and rewrite `audit_chain_heads`. The `REVOKE` in `deploy/init-db.sql` is commented out and the migration has no triggers or grants. | Reproduced on real PostgreSQL as `app_rw` |
| F2 | Audit writer races on first write to a new chain and under load. 50 concurrent writers on one chain gave 6 `IntegrityError`s; 20 concurrent first writes to a new chain gave 12 failures. The chain stayed valid. No concurrency test exists. | Reproduced |
| F3 | Claim-scoped mutations do not call `check_claim_access`: `make_decision`, `create_assessment`, `review_document`, `transition_status`, and all investigation mutations (`assign_investigator`, `start_review`, `add_finding`, `escalate_investigation`, `complete_investigation`). Only reads do. | Read |
| F4 | Customers and staff can read risk evaluations. `tests/integration/test_risk_service.py` (around line 260) asserts that customers can read them. | Read |
| F5 | `decision_service.make_decision` reads coverage without a row lock, increments `consumed_amount` in Python, never checks remaining limit at approval, silently skips the ledger if no coverage row exists, and has no reversal path. No claim row lock in the decision path. | Read |
| F6 | `claim_limit_policy` is hard-coded to `CAP_WITH_WARNING` in `app/engine/validation.py` and `ruleset_v1.yaml` without an owner decision. | Read |
| F7 | `scripts/replay.py` rebuilds inputs from the current database, not from stored `inputs_snapshot`. Later data changes cause false mismatches or hide differences. | Read |
| F8 | Document review does not trigger revalidation. Two different sensitive-document lists exist (`claim_service.SENSITIVE_DOC_CODES` and an inline tuple in `app/security/rbac.py`). Upload does `await file.read()` before the size check. | Read |
| F9 | Audit serializer is a denylist of field names; arbitrary keys and free text pass. Document review remarks go into audit event data. | Read |
| F10 | `tests/conftest.py` hardcodes in-memory SQLite. CI starts PostgreSQL but only runs migrations on it. The backup drill can fall back to SQLite or a seeded temporary database. No coverage threshold (`--cov-fail-under`) is enforced. | Read |
| F11 | Missing: `scripts/backtest.py`, `scripts/retention_report.py`, external audit anchor export, scheduler, UI templates, production deployment artifacts (only a dev compose file). | Read |
| F12 | A claim whose indicators are mostly skipped can score 0 and look LOW. MEDIUM and HIGH route identically because the investigation threshold equals the MEDIUM lower bound, and one early claim (weight 25) routes to investigation alone. | Read |
| F13 | Phase reports in `decisions.tsv` claim 14/14 acceptance items and "append-only audit" although F1, F2 and F5 hold. | Read |

---

## 5. Environment (do this first)

1. Record: `git --version`, `python --version`, `docker --version`, `docker compose version`, current directory, `git status`.
2. If a clone exists, inspect the branch and uncommitted changes. Preserve all user work. Do not reset, clean or overwrite. Then `git fetch origin`.
3. `git checkout -b hardening/v1 origin/main` (or the existing branch if it exists).
4. Install: `pip install -e ".[dev]"` (run in the repo root).
5. PostgreSQL 16 is required for integration work. Preferred: `docker compose -f deploy/docker-compose.yml up -d db`. If Docker is unavailable, `pip install pgserver` gives a local PostgreSQL for verification. That is NOT proof of PostgreSQL 16; CI or Docker must still run on 16. State exactly which you used.
6. Roles: the database must have at least `migrator` (DDL), `app_rw` (application), `audit_verifier` (read-only) and, for anchoring, a separate writer with different credentials. Update `deploy/init-db.sql` and the migrations so bootstrap and migrations agree.
7. If any tool or service is unavailable, say exactly what and why. Never fabricate results.

---

# PHASES

Every phase report must contain: status (PASS / PARTIAL / FAIL / BLOCKED), files changed and why, exact commands and outputs, security impact, database impact (migrations), requirement traceability (labels from rule 5), remaining work, and "Unrequested changes".

---

## P0: Baseline, reproduction and traceability

No behaviour changes.

Tasks:
1. Run the full baseline on the base commit: Docker build, compose start, PostgreSQL connectivity, `alembic upgrade head`, `downgrade base`, `upgrade head`, app start, `/healthz` and `/readyz`, login, `pytest --cov`, ruff (check and format), mypy, lint-imports. Save complete outputs to `docs/evidence/baseline/`.
2. Reproduce F1 to F13. Save scripts and outputs. Mark each as Reproduced / Read / Disproved.
3. Create `docs/ROUTE_MATRIX.md`: every route × allowed roles × object-level rule × data returned.
4. Create `docs/TRACEABILITY.md`: requirement → code → test → enforcement label.
5. Create `docs/DECISIONS.md` (Section 3) and `docs/PROGRESS.md`.
6. Reconcile `goal.md`, `README.md`, `docs/*.md` with the code. List every contradiction.

Gate: baseline outputs saved; every finding reproduced or disproved; route matrix and traceability complete; owner reviews and fills decisions.

---

## P1: Security and authorization

Objective: no unauthorized access to any claim, document or risk data.

Tasks:
1. **Service-level authorization.** Every claim-scoped service method (reads and mutations) must authorize the actor inside the service, not only in the route. Fix F3. Add a test that fails if any public service method touching a claim lacks the check (inspect the AST or use an explicit registry).
2. **Role matrix (per D6, D7).** Implement exactly the approved matrix. Default if approved as recommended:
   - customer: own records only, never risk data; customer-facing status labels must not reveal investigation (map UNDER_INVESTIGATION and ESCALATED to a generic "under review").
   - staff: summary for all claims, full detail only for claims they registered; no risk data; no document content.
   - assessor: claims in assessment; risk category and completeness only, no indicator evidence.
   - investigator: assigned investigations only; full risk evidence.
   - manager: everything, including assignment and escalation.
   - admin: configuration and user management; no claim decisions; risk data only through audited, explicit access.
3. **Risk visibility (fix F4).** Enforce in the service layer and serializers, per role. Cover every response that could leak risk fields: claim detail, timelines, validation results, analytics, CSV export. Change the existing test so customers get 403.
4. **Documents.** One source of truth for sensitive document codes (fix F8) used by access control, metadata redaction, download and audit. Sensitive content visible only to roles allowed by D8. Download only through an authorized endpoint (no direct file URLs, no presigned URLs for sensitive documents).
5. **Access log.** Create an append-only `access_log` table (no UPDATE/DELETE/TRUNCATE for the app role) recording every sensitive document view/download and every non-owner sensitive-record view. Write and commit the log row BEFORE streaming. If the write fails, return 503 with no bytes (fail closed). One bounded immediate retry. Do not log content, only ids.
6. **Upload hardening.** Enforce size while reading (reject on `Content-Length` and on cumulative bytes before buffering all of it). Detect type from file signature (PDF, JPEG, PNG). Reject any mismatch between declared and detected type. Random storage key, sanitized filename, storage outside any web root, path-traversal tests.
7. **Sessions and abuse.** Cookie `HttpOnly`, `Secure`, `SameSite`; session rotation on login; revocation; expiry. DB-backed login rate limiting that is correct under concurrent requests. Generic login error messages. CSRF on state-changing requests. Security headers: CSP, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, HSTS in production.
8. **Separation of duties.** Assessor ≠ decider (exists). Add: document reviewer ≠ uploader. Investigator cannot assess or decide the claim they investigated.

Required tests (adversarial, PostgreSQL where relevant):
- For every route and service operation: other customer, unregistered staff, unassigned investigator, assessor, admin, unauthenticated.
- Risk payload redaction per role.
- Sensitive document access and access-log entry for every sensitive code; access-log failure means no bytes.
- Spoofed MIME, oversize body, malformed file, path traversal, unauthorized download.
- Concurrent login attempts against the rate limiter.

Gate: all security tests pass; no known unauthorized access to objects, risk data or documents; route matrix updated.

---

## P2: Transactional approval and coverage accounting

Objective: coverage limits cannot be overspent under concurrency, and approval, ledger, status and audit are atomic.

Tasks:
1. **Lock order, documented and tested:** claim row, then coverage rows sorted by id, then other business rows, then audit chain locks last (sorted by chain key). Set `lock_timeout`; retry a bounded number of times on deadlock or serialization failure; operations must be idempotent.
2. **Claim row lock** (`SELECT ... FOR UPDATE`) for every pipeline step that reads or changes workflow state, plus `status_version` stale-client detection returning 409.
3. **Approval transaction** (fix F5): lock the claim and the coverage row; fail if no coverage row exists (never skip the ledger); recompute remaining capacity from authoritative state by `limit_scope` (PER_CLAIM uses the limit; PER_POLICY_PERIOD uses limit minus consumed); recompute payable P′ from the stored assessed loss with the engine; require `0 < approved ≤ P′`; insert CONSUME ledger row; update `consumed_amount` with a SQL expression under the lock (not read-modify-write in Python); write decision, status history, audit in the same transaction.
4. **Database protection:**
   - `coverages`: `CHECK (consumed_amount >= 0 AND consumed_amount <= limit_amount)` for PER_POLICY_PERIOD.
   - PER_CLAIM: decisions store a `limit_applied` snapshot with `CHECK (approved_amount <= limit_applied)`.
   - Ledger: amounts > 0; unique per `(decision, entry_type)`; a REVERSE references exactly one CONSUME; append-only grants and triggers.
5. **Reconciliation:** script and scheduled job proving `consumed_amount` equals the ledger sum; safe to rerun; a mismatch creates an incident record and never auto-fixes.
6. **D1 policy (decided).** Remove the hard-coded `CAP_WITH_WARNING` default (fix F6). `limit_exceeded_behavior` is a NOT NULL coverage field (CAP or REJECT) with no default; unknown values are errors. Missing or unknown coverage terms fail validation with `POLICY_TERMS_INCOMPLETE`. Under CAP, validation V11 fails only when the remaining limit is zero (`LIMIT_EXHAUSTED`) and a request above the remaining limit is a warning; payable is capped at assessment and approval, and the reason code `LIMIT_APPLIED` is recorded automatically. Under REJECT, V11 fails with `LIMIT_EXCEEDED`. The limit applies to the payable amount after deductible and copay. Add the `require_manager_when_capped` setting (default off).
7. **Reopen/correction (decided).** Manager-only transition from APPROVED or REJECTED to READY_FOR_ASSESSMENT with mandatory remarks, a compensating REVERSE ledger row (referencing exactly one CONSUME), and the old assessment superseded; the original decision and ledger rows are never edited. All other roles cannot change a final claim.

Required tests (PostgreSQL):
- 2 and 50 concurrent approvals at exactly the remaining limit: total never exceeds the limit; `consumed_amount` equals ledger sum.
- Approval above remaining capacity is refused.
- Audit or ledger write failure rolls back status, decision, ledger and consumed amount (assert unchanged).
- Stale `status_version` and concurrent transitions.
- Zero deadlocks (check the PostgreSQL log) under mixed concurrent load.
- Approval with no coverage row fails.

Gate: no overspend under concurrency; decision, status, ledger and audit are atomic.

---

## P3: Audit integrity

Objective: tamper-evident, append-only, concurrency-safe audit with external anchoring. Items tagged [CORE] and [D4] are both required: D4 is decided (approved).

Tasks:
1. [CORE] **Database-level append-only (fix F1).** App role has no UPDATE, DELETE or TRUNCATE on `audit_log`, `claim_status_history`, `decisions`, `investigation_findings`, `coverage_ledger`, `access_log` (and chain state). Add triggers raising on UPDATE, DELETE and TRUNCATE as defense in depth. Bootstrap SQL and migrations must agree. Test as the real `app_rw` role.
2. [CORE] **Concurrency-safe appends (fix F2).** Remove the first-write race. Serialize per chain, retry on conflict, 50+ concurrent writers on one chain and 50 concurrent first-writes to new chains, then full verification proving no gaps or forks and zero errors.
3. [CORE] **Strict allowlist (fix F9).** Replace the denylist with a per-action schema: unknown keys rejected, free text rejected (no remarks, descriptions, findings, names), values limited to ids, enums, dates, decimals-as-strings and hashes. Test nested and disguised fields. Add a canary test: seed distinctive strings in every personal field, run the full pipeline, and scan all audit rows, snapshots and logs for them.
4. [CORE] **Chains.** Per-claim chains plus a `system` chain. [D4] Extend to `<entity>:<id>` chains for claim, customer, policy and user; `system` only for genuinely system-level events (ruleset activation, configuration, key rotation, migration and import summaries, anchor and verification results, chain-break acknowledgments, retention reports, backup drills). Claim-path operations must never write to `system`. Failed logins go to the login-attempts table, not to a chain.
5. [CORE] **HMAC.** Key from the environment or secret manager, never in the database or repository. Genesis `sha256("genesis:" + chain_key)`. Canonical JSON: sorted keys, UTF-8, no whitespace, decimals as fixed-scale strings, UTC timestamps with microseconds and `Z`, lowercase UUIDs, floats rejected. [D4] Use length-prefixed HMAC framing over an explicit field list (Appendix B), `hash_version` and `key_id` in the hashed content, stored canonical payload text as the source of truth, key rotation with verify-only old keys, key escrow documented separately from database backups.
6. [D4] **Single write path.** The app role cannot insert into `audit_log` directly. Writes go through SECURITY DEFINER database functions with fixed `search_path`, and a BEFORE INSERT trigger enforces seq, `prev_hash`, genesis and known `key_id`. Per-chain serialization with transaction-scoped advisory locks taken in sorted order. Composite foreign key and CHECKs per Appendix B. Remove the head table, or if kept: INSERT allowed only as an empty head, UPDATE forward-only by exactly one, writes only through the function.
7. [D4] **Per-entity deferred audit triggers.** DEFERRABLE INITIALLY DEFERRED constraint triggers on every audited business table require, at commit, an audit row for the same transaction, entity table, entity key, operation and expected chain, with an allowed action; for claims, `status_after` must match the final status. A schema test lists every table as audited or explicitly exempt. Migrations that change data must write a summary event. Document honestly what this does not prove (accuracy of the payload).
8. [CORE] **External anchoring.** Daily job writes a manifest (timestamp, previous manifest hash, changed chain heads, root hash) to the destination chosen in D3, using credentials that cannot overwrite or delete prior anchors. The verifier fetches the latest manifest automatically and checks every anchored `(chain, seq, hash)` still exists and every head is at or beyond it. Manual anchor input stays only as an explicit diagnostic override. Include the manifest watermark so in-flight transactions are not missed.
9. [CORE] **Verification.** Read-only DB role. Incremental nightly and full weekly through the platform scheduler or cron (no Celery or Redis). Record `verification_runs`. On a break: raise an incident record, never repair or rewrite, block new writes to the affected chain until a signed `CHAIN_BREAK_ACKNOWLEDGED` event.
10. [D4] **Access log integrity.** Per-row HMAC (no chain, so no contention), and each daily manifest includes the row count and digest of the closed day's `row_hash` values; the verifier recomputes them.

Required tests (PostgreSQL, as the real application role):
- UPDATE, DELETE, TRUNCATE and DROP attempts rejected on every protected table.
- Tamper matrix: row mutation, row deletion, sequence gap, fork attempt, stale head, tail truncation (detected via anchor), missing anchor, anchor mismatch, unknown `key_id`, rolled-back `hash_version`, row spliced from another chain.
- The 10-claims-updated-with-1-audit-event case fails at commit [D4]. Also: insert without audit, wrong operation, wrong chain, event from another transaction, status mismatch, savepoint cases, TRUNCATE denied, migration with and without summary event, 50 concurrent mixed transactions without false failures or deadlocks.
- An injected audit-write failure rolls back the business change.
- Every `audit_log` column is either in the HMAC input or on an explicit not-hashed list; a test fails if a column is added without classification.
- Restore from backup still verifies (with the key).

State plainly in docs: the log is tamper-evident for history older than the last anchor, and an application compromise can still write new forged-looking entries.

Gate: DB-level immutability, concurrency proof, tamper detection and external anchoring all pass on PostgreSQL.

---

## P4: Documents and claim lifecycle

Tasks:
1. **V13.** Every mandatory document type needs at least one VALID document. Separate reason codes `DOCUMENT_MISSING`, `DOCUMENT_PENDING_REVIEW`, `DOCUMENT_INVALID`; when V13 is the only failing rule the result is BLOCKED_DOCS with the exact documents listed.
2. **Statuses:** PENDING_REVIEW, VALID, INVALID, SUPERSEDED (MISSING is derived). A replacement is a new row; the old row becomes SUPERSEDED. Never overwrite or delete; file-defining columns (hash, storage key) immutable (revoked UPDATE plus trigger).
3. **INVALID** needs a reason code and remarks (CHECK). Review transitions limited to legal states and authorized roles (D8). Reviewer ≠ uploader.
4. **Revalidation (fix F8).** Review, upload and replacement trigger revalidation in the same transaction, with correct claim locking.
5. [D4] **Identity attestation.** Append-only revision table keyed `(document_id, rev)`: `prev_rev = rev - 1`, composite FK to the previous revision and to `(document_id, document_sha256)`; "current" is the highest revision (derived, never a flag); result CONFIRMED / MISMATCH / UNCERTAIN; reason code and remarks required for MISMATCH and UNCERTAIN; reviewer id and role snapshot; `subject_identity_version` must match the subject's current version or the attestation counts as stale. Replacing a document never carries over an attestation. Concurrent reviews: lock the document row, require `expected_rev`, lost race returns 409. The record never contains a name. V13 for identity-bearing documents requires VALID and a current CONFIRMED attestation.
6. **State machine** (add the missing edges, every edge in one table, with a test that tries every pair): DOCUMENTS_PENDING to REJECTED (human decision, remarks and reason code); READY_FOR_ASSESSMENT to REJECTED and INFO_REQUESTED; INFO_REQUESTED re-evaluation again requiring investigation opens a NEW investigation linked to the previous one (never auto-skip because an earlier one cleared the claim); [D4] RISK_DATA_PENDING. Manager reopen only if D1 approves it. UNDER_INVESTIGATION/ESCALATED must always match the investigation status (invariant test). No automatic expiry or closure of stale claims: add a stale-claims report for humans and a test proving no scheduled job changes a claim status.
7. R08 uses only VALID (and, [D4], attested) document metadata.

Tests: every document state and transition, all illegal transitions, replacement history, revision linearity (concurrent submits, gaps, cross-document references, hash mismatch, stale subject version, uploader = reviewer), revalidation after review, BLOCKED_DOCS listing.

Gate: document lifecycle and state-machine suites pass; pending or invalid metadata cannot influence trusted risk evidence.

---

## P5: Assessment engine, replay and snapshots

Tasks:
1. **Scope (D5, provisional).** A+ applies: one claim = one coverage category; assessment holds typed components defined by the product profile; the assessor sets `assessed_loss` (≤ requested) and may zero components with a reason; the engine computes `payable_amount`; approved amount ≤ payable. Add nullable `incident_group_id` (D12). Document in `docs/SCOPE.md` exactly what v1 cannot process: claims settled across two or more coverage categories in one calculation; shared or aggregate deductibles; independent per-item decisions; interim or part payments, recoveries, subrogation, salvage netting; policy-level aggregate limits; mid-term endorsements.
2. **Product profile registry** in code for HEALTH, VEHICLE, PROPERTY: input schema, ordered steps, extra validation rules. Reject unsupported products; no generic fallback. Step order: HEALTH deductible, copay, limit cap; VEHICLE depreciation, deductible, limit cap; PROPERTY coinsurance factor `min(1, sum_insured / value_at_risk)`, deductible, limit cap.
3. **Decimal rules.** Parse every monetary and rate value from YAML as a string into `Decimal`; reject floats. No intermediate rounding; round once at the end with `ROUND_HALF_UP` to 2 places. Copay stored as `NUMERIC(5,2)` percent 0 to 100. Return a step-by-step trace.
4. **Illustrative formulas stay labelled `ILLUSTRATIVE_DEFAULTS`** until D11 is signed (vehicle depreciation by part category and age, coinsurance, room-rent sub-limits and similar are insurer-owned rules). Build the mechanism and golden tests; do not present the numbers as insurer-approved.
5. **Coverage terms are NOT NULL with explicit values** (deductible 0 and copay 0 must be explicit; no silent default; unknown enum values rejected). Imports with missing terms go to `import_rejects`. Coverage edits are blocked once a claim references the coverage.
6. **Boundary tests:** loss at or below deductible gives 0; remaining limit 0 gives 0; copay 100% gives 0; depreciation above loss; zero or tiny value at risk; every product-specific limit.
7. **Insured item and member verification:** the claimed vehicle or property matches the policy's insured item; the claimant is an insured member. Derive claim type from the policy; remove the redundant claim-type mismatch logic only through a migration with compatibility tests.
8. **Replay from stored snapshots (fix F7).** Validation, risk and assessment runs store their full structured input snapshot, `ruleset` version and content hash, `engine_version`, output and trace. `scripts/replay.py` recomputes from the STORED snapshot only, never from live tables, and compares outputs (including trace and messages). Engine behaviour changes require an `engine_version` bump; golden fixtures are pinned per engine version and CI fails if outputs change without a bump. Keep ruleset version and engine version separate.
9. **Snapshot content allowlist.** Allowed: internal UUIDs, enums, dates, decimal strings, coverage terms in force, counts and window aggregates, product numeric inputs (component codes, part category, vehicle age band, sum insured, value at risk), hashes. Never: names, exact date of birth, addresses, contact data, government ids, policy or registration numbers, clinical text or codes, free text, file names or content, IP addresses. If a product rule needs age, a derived value may be included only if the ruleset declares it and the owner approves it. Enforce with typed models that forbid extra fields and the canary test.
10. **Business timezone.** One configured timezone, immutable for v1: store `registered_at` (UTC), `registered_on` (business date) and the timezone on the claim; refuse to start if the configured timezone differs from the recorded one. Engines receive dates, never timestamps. Test the UTC/local midnight boundary.
11. **Old-version replay.** Provide a release manifest (commit SHA, lockfile hash, Python version, base image digest, ruleset hash) created in CI. Provide a replay runner that executes only `engine/`, runs offline with no secrets or database credentials, reads a sealed bundle (snapshots, ruleset text, expected outputs, hashes) and writes a comparison report. Old application versions must never be run as services.
12. **A+ sample-review tooling (D13).** Write `docs/SAMPLE_REVIEW_PROTOCOL.md` (the protocol and starting thresholds from D13), a CSV template for reviewers (claim reference or pseudonymous id, product, value band, classification FIT / SPLITTABLE / NO-FIT, reason code, reviewer, adjudicated result), and `scripts/aplus_fit_report.py`, which reads the completed CSV and reports shares by count and by value per product with Wilson 95% intervals and a pass or fail against the thresholds. The client runs the review; you only build the tooling and document it.

Gate: golden and boundary tests pass; replay from stored snapshots passes; no unsupported-product fallback; snapshots pass the canary test.

---

## P6: Risk governance

Tasks (values from D9; Appendix A lists pilot defaults):
1. **Ruleset schema.** Every indicator has `rationale`, `enabled`, `group`, `weight` (integer), `sufficient_alone`, `evidence_level` (`NONE` or `REVIEWED`) and applicable products. Every group has an integer `cap`. A validator rejects ungrouped indicators, floats and unknown keys. Do not name any level VALIDATED. Show a PILOT banner wherever risk results appear for authorized roles and in exports.
2. **Ruleset lifecycle.** DRAFT, then APPROVED (four-eyes: approver ≠ author, evidence reference and backtest report hash attached), then ACTIVE (audited, hash recorded). Never edit an active ruleset file in place (CI hash check against the `rulesets` table). Each active ruleset has a review date. Running with `evidence_level NONE` in production requires a recorded risk acceptance by a named business owner.
3. **Order of operations (exact):** load ruleset and engine version; build typed `RiskInput`; evaluate indicators in fixed id order; apply group caps (`capped = min(cap, sum of triggered weights)`); `score = min(100, sum of capped groups)`; assign category; compute routing `requires_investigation = score >= investigation_threshold OR any triggered indicator with sufficient_alone`; store routing reasons.
4. **Indicator statuses:** TRIGGERED, NOT_TRIGGERED, SKIPPED (typed reason), NOT_APPLICABLE, DISABLED. Only TRIGGERED and NOT_TRIGGERED count as evaluated.
5. **Completeness.** `applicable_weight` = sum of weights of enabled, applicable indicators; `evaluated_weight` = the same over evaluated ones. COMPLETE if equal; PARTIAL if `evaluated_weight * 100 >= partial_min_pct * applicable_weight` and every `mandatory` indicator is evaluated; otherwise INSUFFICIENT_DATA. INSUFFICIENT_DATA means category `UNASSESSED` and NULL score (CHECK enforced), never LOW. The API returns one composed label so a UI cannot show a category without its completeness. [D4] Add the `RISK_DATA_PENDING` state; a human completes the data (re-evaluation) or a manager records a completeness waiver with remarks. Missing data never causes investigation routing by itself.
6. **History coverage (D14).** The `history_coverage` table with a per-product `loaded_from` date; an indicator whose lookback starts before it is SKIPPED with `HISTORY_WINDOW_NOT_COVERED` (missing history must not look like "no prior claims").
7. **Windows.** Lookbacks use the configured business date derived from `registered_at`; exclude the evaluated claim and WITHDRAWN claims; document inclusive and exclusive boundaries; frequency indicators count distinct incidents, evidence shows both claim and incident counts.
8. **Protected attributes and proxies.** `RiskInput` is a typed model containing none of age, gender, religion, ethnicity, caste, disability or marital status (a test inspects the schema). Remove the geographic clause from R08; territory-of-cover belongs in validation as a coverage condition. R08 reads the human attestation result ([D4]) instead of comparing name strings.
9. **Incident groups (D12, decided: include):** customers may only suggest a link (the engine ignores it); staff or investigators propose; an assessor or manager confirms (proposer ≠ confirmer); all members must belong to the same customer (composite foreign key); limits on date span and group size; one current membership per claim (partial unique index); the confirmer cannot later decide any claim in the group; unlink, merge and split are manager-only with reason and remarks; membership is append-only revisions and freezes when the claim reaches a final state; a correction re-evaluates the customer's OPEN claims and may add investigation routing but never cancels an open investigation; every group operation is audited as a multi-chain event.
10. **Manual referral.** A manager or assessor may refer any claim to investigation with mandatory remarks (`MANUAL_REFERRAL`).
11. **Wording and no auto-reject.** A test scans templates, API messages and exports for fraud-declaring words. A test proves no code path turns a score or category into REJECTED.
12. **`scripts/backtest.py`.** Reports overall and per-indicator flag rates, co-occurrence, stability by month and product, threshold sensitivity, expected investigator workload, and, only among claims actually investigated, concern rates for flagged versus unflagged with the selection-bias caveat printed. It must NOT report accuracy, precision, recall or false-positive rate against true fraud. The report template carries a fixed disclaimer and a test forbids the words "accuracy" and "detection rate". Include an optional disparity screen by available segments (whether protected attributes may be used for monitoring is an owner and legal decision).
13. Optional and cheap: investigators mark each triggered indicator relevant or not relevant at closure (structured feedback for later governance).

Tests: the worked examples in Appendix A as fixtures; skipped and disabled paths; group cap; completeness states and UNASSESSED never shown as LOW; replay identical; role-specific visibility; protected-attribute schema test; wording test; no-rejection-from-risk test.

Gate: all of the above pass; risk output is reproducible from stored snapshots.

---

## P7: PostgreSQL tests, CI, imports and operations

Tasks:
1. **pytest on PostgreSQL (fix F10).** Replace the SQLite fixtures with PostgreSQL (per-test transaction rollback or schema reset). SQLite only for isolated utility tests with a written justification. PostgreSQL-dependent tests fail (not skip) if the database is missing.
2. **CI.** Jobs: lint, format check, mypy, import-linter; PostgreSQL service with migration up/down/up and the FULL suite; `--cov-fail-under` (engine ≥ 90%, overall ≥ 80%); dependency audit (`pip-audit`); SAST (`bandit` or `semgrep`); container image scan; secret scanning; OWASP ZAP baseline against staging (runs when a staging URL is configured, otherwise reported as NOT RUN, never as passed); the backup/restore drill on PostgreSQL.
3. **Imports.** Versioned CSV/JSON schema; dry-run; idempotent upserts; transaction atomicity (chunked, with a summary event); `UNIQUE(source_system, external_ref)` where both are populated, for customers, policies and claims; reject missing coverage terms into `import_rejects`; history load sets `history_loaded_from`. Tests for duplicates, partial failure rollback, re-run idempotency.
4. **Antivirus hook.** An integration point only. Report scanning as INACTIVE until a real scanner is configured and verified.
5. **`scripts/retention_report.py`.** Report-only, never deletes, retention classes and default periods from configuration (D10 defaults), respects a `legal_hold` flag. Build nothing that deletes data.
6. **Scheduler jobs** through platform scheduler or cron (no Celery or Redis): nightly incremental and weekly full audit verification, daily anchor, reconciliation, session and login-attempt cleanup, backup, retention report. Idempotent, recorded in `job_runs`, with a missed-run alert. No job may change a claim status (test).
7. **Backup and restore drill, PostgreSQL only.** `pg_dump`, restore into an isolated database, compare row counts of all domain tables, verify every audit chain on the restored database (the HMAC key must be available separately), record timings. Never fall back to SQLite or a seeded substitute for the production acceptance drill. Run it in CI or a controlled staging pipeline.
8. **Roles and keys.** Separate DB roles (`migrator`, `app_rw`, `audit_verifier` read-only, anchor writer with its own credentials). Document encryption in transit, at rest and for backups, key backup and escrow, and the incident procedure for a suspected key compromise.
9. **Operational docs.** Update `docs/RUNBOOK.md`: deploy, rollback, backup and restore, rotating secrets, adding a user, activating a ruleset, verifying the audit chain, handling a chain break, reconciliation mismatch, common failures. Document as client responsibilities (not built): lawful basis and consent wording, retention schedule, erasure procedure, breach notification, DPIA, and the independent penetration test before go-live.

Gate: the full suite and the drill pass on PostgreSQL in CI; the security pipeline runs; coverage thresholds are enforced.

---

## P8: Product surface and deployment

Tasks:
1. **D2 (decided: UI).** Build server-rendered Jinja2 plus minimal JavaScript (HTMX allowed), no SPA. Pages per role for registration, documents, claim list and detail, assessment, investigation, decision, analytics. Authorization on every view and action; customer-safe status labels; no risk data in views the role must not see.
2. **Deployment artifacts (per D3):** production Dockerfile (non-root, base image pinned by digest, healthcheck), production compose or platform manifests, reverse proxy with TLS (for example Caddy), request size limit and timeouts, secrets from a secret manager or environment (never the repo), migration as a separate release step, gunicorn plus uvicorn workers.
3. **Probes and observability:** `/healthz` (process) and `/readyz` (database reachable, ruleset hash matches, migrations current, HMAC key present); JSON logs with request id and no PII; metrics (rate, latency, errors, claims by status); alerts for 5xx rate, database down, failed audit verification, missed jobs, access-log failures, reconciliation mismatch.
4. **Config validation.** Refuse to start in production if `DEBUG` is on, secrets are weak or default, the active ruleset fails schema or hash checks, or the business timezone changed.
5. **CD.** Build once, deploy the same image to staging automatically and to production only after manual approval, with rollback to the previous image tag.
6. **Staging acceptance.** Role-based end-to-end runs for customer, staff, assessor, investigator, manager and admin; a fresh deploy from scratch using only `docs/RUNBOOK.md`; rollback tested; a backup restored once.
7. Do not claim "deployed" or "production ready" without a URL, run logs and evidence. Do not claim a penetration test occurred; it is a client responsibility to commission.

Gate: production-like end-to-end acceptance, deployment and restore evidence, security review and all blocking decisions complete.

---

## 6. Final acceptance checklist

All must be demonstrated with evidence. A phase passing is not enough.

1. A customer registers a claim on their own active policy and gets a unique reference; another customer cannot read it.
2. Missing, pending or invalid mandatory documents stop the claim at DOCUMENTS_PENDING with the exact documents listed; review and replacement trigger revalidation.
3. Every validation rule has a failing example with its reason code.
4. A clean claim passes validation, risk (LOW, COMPLETE), assessment and a human decision with no investigation.
5. A flagged claim is routed to investigation, cannot be assessed or decided until it completes, and no fraud-declaring wording or risk detail reaches unauthorized roles.
6. No path converts a risk score into a rejection (test).
7. Decisions require remarks; REJECTED requires reason codes; separation of duties enforced.
8. Approved amount ≤ payable ≤ engine cap; coverage cannot be overspent under concurrent approvals (PostgreSQL test).
9. The audit log is append-only at DB level for the real application role; the 50-writer test and verification pass; tampering, truncation and anchor mismatch are detected.
10. Replay reproduces stored validation, risk and assessment results exactly from stored snapshots.
11. Every route and every claim-scoped service method has explicit authorization; object-level tests pass.
12. Analytics numbers match a seeded dataset and leak no risk detail.
13. Backup restored on PostgreSQL with all domain rows and audit chains verified; fresh deploy to staging works from the runbook.
14. CI is green on the final commit with coverage thresholds, dependency audit, SAST and image scan.
15. Documentation matches the code. Every exception is written down.

Do not declare completion while any item is unverified.

---

## 7. Start now

1. Run Section 5 (environment), then P0.
2. Create `docs/DECISIONS.md` and `docs/PROGRESS.md`.
3. Finish P0, write the phase report, and stop for owner review.

---

# APPENDICES

## Appendix A: Pilot risk ruleset (illustrative, UNVALIDATED, owner must approve under D9)

These numbers are pilot assumptions. They are not validated fraud-detection thresholds.

| Group (cap) | Indicator | Rule | Weight |
|---|---|---|---|
| FREQUENCY (25) | R01 | at least 3 distinct incidents by the customer in the last 12 months | 20 |
| FREQUENCY | R02 | at least 2 distinct incidents on the policy in the last 6 months | 15 |
| AMOUNT (25) | R03 | requested more than 80% of remaining limit | 20 |
| AMOUNT | R04 | requested more than 3 times the median approved amount for the same product and category (only with at least 10 historical approved claims, else SKIPPED) | 15 |
| AMOUNT | R10 | round-number amount (multiple of 10,000 and at least 50,000) | 5 |
| TIMING (20) | R05 | incident within 30 days of policy start | 15 |
| TIMING | R06 | incident within 7 days of policy end | 10 |
| TIMING | R09 | filed more than 15 days after the incident | 5 |
| DUPLICATE (10) | R07 | same policy, same incident date, amount within 5%; `sufficient_alone` | 10 |
| CONSISTENCY (20) | R08 | a document of the claim has a current identity attestation of MISMATCH | 20 |

Categories: LOW 0 to 19, MEDIUM 20 to 39, HIGH 40 to 100. `investigation_threshold = 40`. Completeness: `partial_min_pct = 60`, mandatory indicators = the TIMING and DUPLICATE groups. MEDIUM claims are not routed; they are visible to managers and the claim's assessor (category only).

Worked examples (use as test fixtures):

| # | Triggered | Calculation | Score | Category | Investigation? |
|---|---|---|---|---|---|
| 1 | R07 only | DUPLICATE 10 | 10 | LOW | Yes (`SUFFICIENT_ALONE:R07`) |
| 2 | R03, R05, R08 | AMOUNT 20 + TIMING 15 + CONSISTENCY 20 | 55 | HIGH | Yes (`SCORE_THRESHOLD`) |
| 3 | R01, R02, R03, R04, R10 | FREQUENCY 35 capped to 25; AMOUNT 40 capped to 25 | 50 | HIGH | Yes |
| 4 | R03, R06 | AMOUNT 20 + TIMING 10 | 30 | MEDIUM | No |
| 5 | R03 triggered; R10 disabled; R04 skipped; R09 triggered with weight 0 | AMOUNT 20 + TIMING 0 | 20 | MEDIUM | No (stored statuses show DISABLED, SKIPPED, zero-weight trigger) |
| 6 | R03, R05, R09 | AMOUNT 20 + TIMING 20 (cap 20) | 40 | HIGH | Yes (boundary: `>=` threshold) |
| 7 | none | 0 | 0 | LOW | No |
| 8 | all indicators SKIPPED | evaluated_weight 0 | NULL | UNASSESSED, INSUFFICIENT_DATA | No; claim waits in RISK_DATA_PENDING |

## Appendix B: Audit design reference [D4]

This is a logical sketch to be proven by migration tests on PostgreSQL. Do not claim a constraint guarantees more than it does.

```
audit_log (
  chain_key   text   NOT NULL,
  seq         bigint NOT NULL CHECK (seq >= 1),
  prev_seq    bigint,
  prev_hash   bytea  NOT NULL,
  hash        bytea  NOT NULL,
  hash_version smallint NOT NULL,
  key_id      smallint NOT NULL,
  event_id    uuid   NOT NULL,
  xact_id     xid8   NOT NULL DEFAULT pg_current_xact_id(),   -- not hashed
  occurred_at timestamptz NOT NULL,                            -- database clock
  actor_id, actor_role, entity_table, entity_pk, op, action, status_after,
  payload_canonical text NOT NULL,                             -- exact hashed bytes
  PRIMARY KEY (chain_key, seq),
  UNIQUE (chain_key, seq, hash),
  UNIQUE (chain_key, prev_hash),
  CHECK ((seq = 1) = (prev_seq IS NULL)),
  CHECK (prev_seq IS NULL OR prev_seq = seq - 1),
  FOREIGN KEY (chain_key, prev_seq, prev_hash)
    REFERENCES audit_log (chain_key, seq, hash)
)
```

- Genesis row: `seq = 1`, `prev_seq` NULL, `prev_hash = sha256('genesis:' || chain_key)`. A multi-column foreign key is skipped when any referencing column is NULL (MATCH SIMPLE), which is intended for genesis only. A non-genesis row cannot dodge it because `prev_seq` is forced non-NULL by the first CHECK and the other columns are NOT NULL. Test whether the genesis value can be checked in a CHECK on the target PostgreSQL version; otherwise the trigger and verifier check it.
- HMAC input: a domain-separation constant and an environment id, then length-prefixed fields in a fixed order: `hash_version`, `key_id`, `chain_key`, `seq`, `prev_hash`, `event_id`, `occurred_at`, `actor_id`, `actor_role`, `entity_table`, `entity_pk`, `op`, `action`, `status_after`, and the SHA-256 of `payload_canonical`. `xact_id` and surrogate ids are excluded (not restore-portable).
- The database guarantees (with the grants and triggers in place): no duplicate seq, no fork, every non-genesis row links to an existing predecessor with that exact hash, no gaps, no update, delete or truncate by the app role. It does NOT guarantee: that `hash` is a correct HMAC (the key is outside the database), the truthfulness of the payload, or protection against tail removal by an owner or superuser (anchors and the verifier cover that).
- Write path: one transaction takes business locks (claim, coverage sorted), applies the change, then for each chain key in sorted order takes the transaction-scoped advisory lock, inserts the audit row through the function, commits. A failed audit insert rolls back the whole transaction.

## Appendix C: Illustrative calculation examples (ILLUSTRATIVE_DEFAULTS, insurer sign-off required)

- HEALTH: assessed loss 100,000; deductible 5,000 gives 95,000; copay 10% gives 85,500.00; remaining limit 200,000, no cap. Payable 85,500.00. Rounding case: loss 10,001.55, no deductible, copay 12.5%: 10,001.55 × 0.875 = 8,751.35625, rounds once to 8,751.36.
- VEHICLE: parts 60,000 with 20% depreciation gives 48,000; labour 20,000; total 68,000; deductible 2,500 gives 65,500.00.
- PROPERTY: loss 800,000; sum insured 5,000,000; value at risk 8,000,000; factor 0.625 gives 500,000; deductible 25,000 gives 475,000.00.
- Limit cap: HEALTH adjusted amount 85,500 with remaining limit 60,000 gives payable 60,000, `limit_capped = true`, shortfall 25,500 (reason code `LIMIT_APPLIED` recorded).
- Concurrency: limit 100,000, two claims each with payable 80,000 assessed at the same time. The first approves 80,000. The second's approval recomputes remaining 20,000 and payable 20,000; approving 80,000 is refused.

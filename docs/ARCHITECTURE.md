# System Architecture (v1)

## Overview

The Insurance Claim Processing & Fraud Indicator Platform is a deterministic, rule-driven system designed to handle claim registration, automated validation, risk indicator evaluation, assessment, and human decision workflows with an immutable, cryptographically verifiable audit trail.

```
                    +------------------------+
                    |  Web UI / REST API     |
                    +-----------+------------+
                                |
                                v
                    +------------------------+
                    | Service Orchestration  |
                    +-----+------------+-----+
                          |            |
         +----------------+            +----------------+
         |                                              |
         v                                              v
+-----------------------+                    +--------------------+
|  Deterministic Engine |                    | Database & Audit   |
|  - Validation (pure)  |                    | - PostgreSQL 16    |
|  - Risk Score (pure)  |                    | - HMAC Hash Chains |
|  - Assessment Cap     |                    | - Coverage Ledger  |
+-----------------------+                    +--------------------+
```

## Core Architectural Invariants

### 1. Deterministic Pure Engines (`app/engine/`)
- All functions inside `app/engine/` are strictly pure: given identical inputs, they yield byte-identical outputs.
- Prohibited imports: `datetime.now`, `date.today`, `time`, `random`, `uuid`, `os`, `requests`, `httpx`, `sqlalchemy`, `fastapi`.
- Mechanically enforced via `import-linter` contracts and AST inspection test suites.
- Clocks and business dates are explicitly passed into engine routines.

### 2. Independent Audit Chains & Privacy (`app/audit/`)
- Audit events are partitioned into independent hash chains:
  - Per-claim chains: `chain_key = "claim:<claim_id>"`
  - System chain: `chain_key = "system"`
- Chain heads are maintained in `audit_chain_heads(chain_key, last_seq, last_hash)`.
- Concurrent appends serialize per chain by locking the chain head row using `SELECT ... FOR UPDATE`.
- Cryptographic hash formula:
  $$\text{hash} = \text{HMAC-SHA256}(K, \text{canonical\_json}(\text{row}\setminus\{\text{hash}\}))$$
  where genesis hash is $\text{SHA256}(\text{"genesis:"} + \text{chain\_key})$.
- Canonical JSON rules: sorted keys, UTF-8, no whitespace, Decimal as fixed-scale strings, UTC timestamps with microseconds and `Z`, lowercase UUIDs, floats rejected.
- PII and sensitive data are strictly filtered: raw passwords, national IDs, medical diagnoses, and document contents are rejected by the allowlist serializer.

### 3. Coverage Ledger & Limit Accounting (`app/db/models.py`)
- Limits apply to payable amounts post-adjustments.
- `coverages.limit_scope`: `PER_CLAIM` or `PER_POLICY_PERIOD`.
- Database constraint: `0 <= consumed_amount <= limit_amount`.
- Append-only `coverage_ledger` records `CONSUME` and `REVERSE` events.
- Transactional locking order:
  1. Coverage row (`SELECT ... FOR UPDATE`)
  2. Claim row (`SELECT ... FOR UPDATE`)
  3. Chain head row (`SELECT ... FOR UPDATE`)

### 4. Claim Status State Machine (`app/domain/state_machine.py`)
- Transition matrix enforced in `domain/state_machine.py`.
- Final states: `APPROVED`, `REJECTED`, `WITHDRAWN`.
- Every transition commits `claim_status_history` and an audit entry atomically.

### 5. Role-Based & Object-Level Access Control (`app/security/`)
- Server-side RBAC on every route with default-deny.
- Object-level ownership checks: customers only view own records; staff only view registered/assigned claims; investigators only view assigned cases.
- Separation of duties: an assessor cannot decide; an investigator cannot assess or decide.

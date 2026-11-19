# Project Assumptions and Open Decisions

## Open Decisions

### Claim-Limit Policy (Section A.1)
- Status: Resolved in Phase 4 via configuration toggle `claim_limit_policy` in `app/rules/ruleset_v1.yaml`.
- Default Setting: `CAP_WITH_WARNING` (Option 1 proposed design). When requested amount exceeds remaining coverage limit, Rule V11 passes with informational snapshot `limit_warning: true`, deferring transactional capping to assessment/approval.
- Alternative Setting: `REJECT` (Option 2). When active, Rule V11 fails immediately with reason code `LIMIT_EXCEEDED`. Both behaviors are covered by unit and integration tests.

## Operational Assumptions

1. Business Timezone: Set to `Asia/Kolkata` as default configured timezone for evaluation dates, with UTC storage in database.
2. Currency: Single configured currency code (e.g. INR) enforced across all monetary fields.
3. Limit Scope: Default `PER_POLICY_PERIOD` for standard coverage limits pending final policy decision.
4. Decision Remarks Minimum Length: Configured at 20 characters via `MIN_DECISION_REMARKS_LENGTH` in `app/config.py` and enforced via `chk_decision_remarks_min_length` database constraint.
5. Manager Approval Threshold: Configured at 500,000.00 via `MANAGER_APPROVAL_THRESHOLD` in `app/config.py`. Approval amounts strictly greater than this threshold require the manager or admin role.
6. Analytics Processing Time Metric: Computed via SQL aggregates in hours from claim registration timestamp to earliest recorded decision timestamp. Backlog age buckets are partitioned into <7 days, 7-30 days, and >30 days.



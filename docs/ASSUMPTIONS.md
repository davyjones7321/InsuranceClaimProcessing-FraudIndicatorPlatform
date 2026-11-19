# Project Assumptions and Open Decisions

## Open Decisions

### Claim-Limit Policy (Section A.1)
- Status: Blocked pending owner approval before Phase 4 (V11) and Phase 6 (Assessment and Approval).
- Option 1 (Proposed): Allow a claim whose requested amount exceeds the remaining coverage limit, issue a warning, and cap the payable amount at the remaining limit.
- Option 2: Reject/fail the claim when the request exceeds the remaining limit.
- Action: Phase 1 foundation will not implement limit checks or ledger modifications until this choice is formally selected.

## Operational Assumptions

1. Business Timezone: Set to `Asia/Kolkata` as default configured timezone for evaluation dates, with UTC storage in database.
2. Currency: Single configured currency code (e.g. INR) enforced across all monetary fields.
3. Limit Scope: Default `PER_POLICY_PERIOD` for standard coverage limits pending final policy decision.

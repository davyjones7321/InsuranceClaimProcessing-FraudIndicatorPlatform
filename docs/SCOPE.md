# Platform Scope & Processing Boundaries (v1)

This document specifies the operational and computational scope of v1 of the Insurance Claim Processing & Fraud Indicator Platform under Decision D5 (A+ scope) and Decision D12 (Incident Groups).

## 1. Supported Processing Scope (A+)

Under the A+ architecture:
1. **Single Coverage Category per Claim**: Each claim is registered and processed against exactly one coverage category of a policy.
2. **Typed Assessment Components**: Assessments evaluate typed cost components defined by the product profile (e.g. Parts vs. Labour for Vehicle; Room Rent vs. Procedures for Health; Structure vs. Contents for Property).
3. **Assessor Loss Determination**: The human assessor determines `assessed_loss` ($\le$ requested amount) and may zero individual components with recorded business remarks.
4. **Deterministic Payable Cap**: The pure assessment engine computes `max_eligible_amount` deterministically according to the product profile's ordered calculation steps.
5. **Approval Ceiling**: Approved settlement amount must never exceed `max_eligible_amount` and the remaining coverage limit.
6. **Incident Linking**: Multiple claims arising from the same incident can be linked via an `incident_group_id` for tracking and coordinated assessment.

---

## 2. Explicitly Excluded Scenarios (Out of Scope for v1)

The v1 platform strictly refuses or excludes the following processing patterns:

| Excluded Pattern | Description | Handling in v1 |
|---|---|---|
| **Cross-Coverage Category Settlements** | Settling multiple coverage categories (e.g. Comprehensive + Third-Party liability, or Inpatient + Outpatient) in a single consolidated claim assessment. | Excluded. Must be filed as distinct individual claims per coverage category, optionally linked via `incident_group_id`. |
| **Shared / Aggregate Deductibles** | Deductibles that pool across distinct coverages, family members, or policies. | Excluded. Deductibles in v1 apply strictly per coverage row. |
| **Independent Per-Item Decisions** | Partial approvals or rejections of individual items within a single claim without consolidating into the single claim status. | Excluded. The claim decision is unitary (APPROVED, REJECTED, INFO_REQUESTED). Component zeroing occurs during assessment before claim decision. |
| **Interim / Part Payments** | Phased disbursements, advances, interim checks, or progress payments against an open claim. | Excluded. One final settlement per claim via ledger dispatch. |
| **Salvage Netting & Recoveries** | Offsetting salvage value, scrap recoveries, subrogation inflows, or third-party recoveries directly inside claim settlement accounting. | Excluded. Salvage and subrogation must be managed in external operational systems. |
| **Policy-Level Aggregate Limits** | Shared policy-wide monetary umbrellas that constrain multiple separate coverages under one cumulative cap. | Excluded. Limits in v1 are enforced per coverage row (`PER_POLICY_PERIOD` or `PER_CLAIM`). |
| **Mid-Term Endorsements in Flight** | Retroactive policy or coverage amendments while a claim is actively being processed or assessed. | Excluded. Coverage edits are mechanically blocked once referenced by any claim. |

---

## 3. Product Profiles (v1 Supported Products)

The v1 assessment engine recognizes exactly three product profiles. Any other product type (e.g. `TRAVEL`, `LIFE`, `MARINE`) is rejected without generic fallback:

1. **HEALTH**:
   - Ordered Steps: Deductible $\to$ Copay $\to$ Limit Cap.
   - Formula Source: `ILLUSTRATIVE_DEFAULTS` (pending D11 insurer sign-off).
2. **VEHICLE**:
   - Ordered Steps: Depreciation (applied to parts) $\to$ Deductible $\to$ Limit Cap.
   - Formula Source: `ILLUSTRATIVE_DEFAULTS` (pending D11 insurer sign-off).
3. **PROPERTY**:
   - Ordered Steps: Coinsurance factor $\min(1, \text{sum\_insured} / \text{value\_at\_risk}) \to$ Deductible $\to$ Limit Cap.
   - Formula Source: `ILLUSTRATIVE_DEFAULTS` (pending D11 insurer sign-off).

---

## 4. Acceptance Criteria & Fallback (Decision D13)

Client acceptance of the A+ scope requires executing the sample review protocol documented in `docs/SAMPLE_REVIEW_PROTOCOL.md`. If the review fails the 90% threshold for FIT + SPLITTABLE claims, the platform adheres to the documented fallback sequence in D13.

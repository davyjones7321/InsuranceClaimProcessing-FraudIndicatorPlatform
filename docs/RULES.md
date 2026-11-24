# Platform Business Rules Specification

## 1. Validation Rules (`app/engine/validation.py`)

Validation evaluates a frozen `ValidationInput` against the active ruleset.

| Rule ID | Name | Description | Reason Code |
|---|---|---|---|
| **V01** | Policy Exists & Belongs | Verifies policy ID exists and belongs to claiming customer. | `POLICY_NOT_FOUND` / `POLICY_OWNER_MISMATCH` |
| **V02** | Policy Active | Policy status is `ACTIVE` on evaluation date. | `POLICY_NOT_ACTIVE` |
| **V03** | Incident in Policy Period | `start_date <= incident_date <= end_date`. | `INCIDENT_OUTSIDE_POLICY_PERIOD` |
| **V04** | Valid Incident Date | Incident date is not in the future and `<= registration_date`. | `INCIDENT_DATE_INVALID` |
| **V05** | Filing Window | Filed within configured window (default 90 days from incident). | `FILING_WINDOW_EXCEEDED` |
| **V07** | Coverage Exists | Coverage row exists for claim category and `is_excluded == False`. | `NO_COVERAGE` |
| **V08** | Policy Exclusion Check | No exclusion row matches claim category or incident code. | `EXCLUDED` |
| **V09** | Waiting Period | `incident_date >= start_date + waiting_period_days`. | `WAITING_PERIOD_NOT_MET` |
| **V10** | Positive Requested Amount | `requested_amount > 0`. | `AMOUNT_INVALID` |
| **V11** | Coverage Limit Pre-check | Requested amount vs remaining limit (subject to Section A policy). | `LIMIT_EXCEEDED` |
| **V12** | Mandatory Fields | Incident date, location, description, and amount present. | `MISSING_FIELD` |
| **V13** | Document Lifecycle | Mandatory document types must each have at least one `VALID` document. | `DOCUMENTS_MISSING` / `DOCUMENT_INVALID` / `DOCUMENT_PENDING_REVIEW` |

*Note: V06 is retired per Section E.62 because claim type is derived directly from the policy product profile.*

### Outcome Mapping
- All pass: `overall_result = PASS`, claim advances to `VALIDATED`.
- Only V13 fails (all other pass): `overall_result = BLOCKED_DOCS`, claim transitions to `DOCUMENTS_PENDING`.
- Any other failure: `overall_result = FAIL`, claim transitions to `VALIDATION_FAILED` with all failure reasons recorded.

---

## 2. Risk Indicators (`app/engine/risk.py`)

Risk indicators run only after validation `PASS`. They evaluate a typed `RiskInput` that strictly excludes protected attributes (age, gender, religion, ethnicity).

Score formula:
$$\text{score} = \min\left(100, \sum \text{weight of triggered indicators}\right)$$

Categories:
- `LOW`: 0–24 (advances to `READY_FOR_ASSESSMENT`)
- `MEDIUM`: 25–49 (requires investigation, advances to `UNDER_INVESTIGATION`)
- `HIGH`: 50–100 (requires investigation, advances to `UNDER_INVESTIGATION`)

| Indicator | Description | Trigger Threshold | Default Weight | Status |
|---|---|---|---|---|
| **R01** | Repeated claims by customer | $\ge 3$ non-withdrawn claims in last 12 months | 20 | `PILOT` |
| **R02** | Repeated claims on policy | $\ge 2$ claims on same policy in last 6 months | 15 | `PILOT` |
| **R03** | High amount vs limit | Requested amount $> 80\%$ of remaining limit | 20 | `PILOT` |
| **R04** | Unusual amount vs peers | $> 3\times$ median approved amount for same product + category ($\ge 10$ historical claims required, else `SKIPPED`) | 15 | `PILOT` |
| **R05** | Early claim | Incident within 30 days of policy start | 25 | `PILOT` |
| **R06** | Late-in-policy claim | Incident within 7 days of policy end | 10 | `PILOT` |
| **R07** | Duplicate suspicion | Prior claim on same policy, same incident date, amount within 5% | 30 | `PILOT` |
| **R08** | Inconsistent document data | Document `named_person` differs from insured name, or pre-incident doc dated after incident (from `VALID` docs only) | 20 | `PILOT` |
| **R09** | Late filing | Filed $> 15$ days after incident | 5 | `PILOT` |
| **R10** | Round-number amount | Amount is multiple of 10,000 and $\ge 50,000$ | 5 | `PILOT` |

---

## 3. Assessment Calculations (`app/engine/assessment_cap.py`)

Product profile calculation sequence:
- **HEALTH:** Deductible, Copay, Limit Cap.
  $$\text{net} = \max(0, \text{assessed\_loss} - \text{deductible})$$
  $$\text{payable} = \min(\text{remaining\_limit}, \text{net} \times (1 - \text{copay\_percent} / 100))$$
- **VEHICLE:** Depreciation, Deductible, Limit Cap.
  $$\text{after\_depr} = \max(0, \text{assessed\_loss} - \text{depreciation})$$
  $$\text{payable} = \min(\text{remaining\_limit}, \max(0, \text{after\_depr} - \text{deductible}))$$
- **PROPERTY:** Coinsurance factor $\min(1, \text{sum\_insured} / \text{value\_at\_risk})$, Deductible, Limit Cap.
  $$\text{adjusted} = \text{assessed\_loss} \times \min(1, \text{sum\_insured} / \text{value\_at\_risk})$$
  $$\text{payable} = \min(\text{remaining\_limit}, \max(0, \text{adjusted} - \text{deductible}))$$

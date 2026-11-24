# A+ Client Sample Review Protocol (Decision D13)

This protocol governs the sample review required before production acceptance under Decision D5 and Decision D13. The client executes this review; the engineering team provides the rubric, template, and statistical report tooling.

---

## 1. Objective

To verify whether the "A+" single-coverage claim architecture (one claim = one coverage category with typed assessment components) handles at least 90% of the insurer's actual closed claim volume by count and by settled value, without requiring multi-coverage joint settlement engines.

---

## 2. Sampling Frame

1. **Population**: All claims closed in the most recent 12 rolling months across supported products (HEALTH, VEHICLE, PROPERTY).
2. **Stratification**: Stratified random sample by:
   - Product (HEALTH, VEHICLE, PROPERTY)
   - Value Band:
     - Band 1: Micro / Low ($\le$ 25,000 INR)
     - Band 2: Medium (25,001 to 150,000 INR)
     - Band 3: High (150,001 to 500,000 INR)
     - Band 4: Major / Catastrophic (> 500,000 INR)
3. **Sampling Fraction**: In any stratum with fewer than 400 closed claims, review 100% of claims. In strata with $\ge$ 400 claims, take a random sample of at least 200 claims with a recorded pseudo-random seed.

---

## 3. Reviewer Rubric & Classifications

Each claim in the sample is evaluated independently by Reviewer 1 and Reviewer 2. If their classifications disagree, Reviewer 3 (Adjudicator) makes the final binding classification.

Every claim is assigned one of three classifications:

| Classification | Definition |
|---|---|
| **FIT** | Fits directly into A+: one claim, one coverage category, standard deductible, copay, or depreciation. |
| **SPLITTABLE** | A multi-line incident that can be cleanly processed as several distinct, linked A+ claims (using `incident_group_id`) yielding the exact same total payable amount. |
| **NO-FIT** | Cannot be handled by A+ because it requires joint multi-coverage math, shared deductibles, interim payments, or aggregate limits. |

### NO-FIT Reason Codes

When classified as `NO-FIT`, one of the following standardized reason codes must be assigned:
- `CROSS_COVERAGE_JOINT_CALC`: Calculation requires cross-category offset or joint rules.
- `SHARED_AGGREGATE_DEDUCTIBLE`: Deductible floats across multiple distinct coverages or family members.
- `INDEPENDENT_PER_ITEM_DECISION`: Independent partial approval/rejection per item required.
- `INTERIM_PART_PAYMENT`: Multiple phased progress payments required against single claim.
- `SALVAGE_NETTING_RECOVERY`: Salvage or subrogation netting inside settlement ledger.
- `POLICY_AGGREGATE_LIMIT`: Policy-level overall umbrella limit constraining multiple coverages.

---

## 4. Acceptance Thresholds (Fixed Prior to Review)

Per Decision D13, the following thresholds are fixed and cannot be modified post-hoc:

1. **Count Fit Threshold**:
   $$\text{FIT} + \text{SPLITTABLE} \ge 90.0\% \text{ by count per product}$$
2. **Value Fit Threshold**:
   $$\text{FIT} + \text{SPLITTABLE} \ge 90.0\% \text{ by settled monetary value per product}$$
3. **Cross-Coverage Exclusion Ceiling**:
   $$\text{NO-FIT (caused by cross-coverage or shared aggregates)} \le 10.0\% \text{ by count per product}$$

Statistical confidence is reported using Wilson 95% score confidence intervals.

---

## 5. Fallback Sequence (If Any Product Fails)

If a product fails to meet the 90% threshold, the team executes the fallback sequence in Decision D13 in strict order:
1. Extend A+ with the single missing concept if it accounts for the vast majority of NO-FIT claims.
2. Build multi-line claims for the failing product only.
3. Build multi-line claims for all products.
4. Run A+ with a documented manual escalation procedure for NO-FIT claims.

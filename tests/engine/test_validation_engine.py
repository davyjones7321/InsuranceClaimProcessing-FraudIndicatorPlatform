"""Comprehensive unit and property tests for deterministic validation engine (V01-V13)."""

import random
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from app.engine.types import (
    ClaimInput,
    CoverageInput,
    DocumentInput,
    ExclusionInput,
    PolicyInput,
    RequiredDocumentTypeInput,
    RuleOutcomeEnum,
    ValidationInput,
    ValidationOutcomeEnum,
)
from app.engine.validation import evaluate_validation
from app.rules.loader import ACTIVE_RULESET


def make_valid_claim_input(
    incident_date: date = date(2026, 3, 15),
    registered_date: date = date(2026, 3, 20),
    requested_amount: Decimal = Decimal("1500.00"),
    claim_category: str = "CAT_SURGERY",
    location: str = "Apollo Hospital, Chennai",
    description: str = "Appendectomy procedure",
) -> ClaimInput:
    return ClaimInput(
        claim_id="clm-001",
        claim_ref="CLM-2026-000001",
        customer_id="cust-001",
        policy_id="pol-001",
        claim_category=claim_category,
        incident_date=incident_date,
        incident_location=location,
        incident_description=description,
        requested_amount=requested_amount,
        registered_date=registered_date,
    )


def make_valid_policy_input(
    customer_id: str = "cust-001",
    status: str = "ACTIVE",
    start_date: date = date(2026, 1, 1),
    end_date: date = date(2026, 12, 31),
) -> PolicyInput:
    return PolicyInput(
        policy_id="pol-001",
        policy_no="POL-HEALTH-001",
        customer_id=customer_id,
        product_type="HEALTH",
        start_date=start_date,
        end_date=end_date,
        status=status,
    )


def make_valid_coverage_input(
    claim_category: str = "CAT_SURGERY",
    limit_amount: Decimal = Decimal("50000.00"),
    consumed_amount: Decimal = Decimal("0.00"),
    waiting_period_days: int = 30,
    is_excluded: bool = False,
) -> CoverageInput:
    return CoverageInput(
        coverage_id="cov-001",
        claim_category=claim_category,
        limit_amount=limit_amount,
        consumed_amount=consumed_amount,
        deductible=Decimal("500.00"),
        copay_percent=Decimal("10.00"),
        waiting_period_days=waiting_period_days,
        is_excluded=is_excluded,
    )


_DEFAULT_POLICY = object()


def make_valid_validation_input(
    eval_date: date = date(2026, 3, 20),
    ruleset: dict | None = None,
    claim: ClaimInput | None = None,
    policy: Any = _DEFAULT_POLICY,
    coverages: tuple[CoverageInput, ...] | None = None,
    exclusions: tuple[ExclusionInput, ...] | None = None,
    documents: tuple[DocumentInput, ...] | None = None,
    req_docs: tuple[RequiredDocumentTypeInput, ...] | None = None,
) -> ValidationInput:
    resolved_policy = make_valid_policy_input() if policy is _DEFAULT_POLICY else policy
    return ValidationInput(
        claim=claim or make_valid_claim_input(),
        policy=resolved_policy,
        coverages=coverages
        if coverages is not None
        else (make_valid_coverage_input(),),
        exclusions=exclusions if exclusions is not None else (),
        documents=documents
        if documents is not None
        else (
            DocumentInput(document_id="doc-1", doc_code="DOC_BILL", status="VALID"),
            DocumentInput(document_id="doc-2", doc_code="DOC_REPORT", status="VALID"),
        ),
        required_document_types=req_docs
        if req_docs is not None
        else (
            RequiredDocumentTypeInput(doc_code="DOC_BILL", name="Hospital Bill", is_mandatory=True),
            RequiredDocumentTypeInput(doc_code="DOC_REPORT", name="Medical Report", is_mandatory=True),
        ),
        evaluation_date=eval_date,
        ruleset=ruleset or ACTIVE_RULESET,
    )


# ========================================================
# Unit Tests for Individual Rules V01 - V13
# ========================================================

def test_v01_policy_exists_and_owner_match() -> None:
    # Match: PASS
    inp = make_valid_validation_input()
    out = evaluate_validation(inp)
    v01 = next(r for r in out.rule_results if r.rule_id == "V01")
    assert v01.outcome == RuleOutcomeEnum.PASS

    # Policy None: FAIL with POLICY_NOT_FOUND
    inp_none = make_valid_validation_input(policy=None)
    out_none = evaluate_validation(inp_none)
    v01_none = next(r for r in out_none.rule_results if r.rule_id == "V01")
    assert v01_none.outcome == RuleOutcomeEnum.FAIL
    assert v01_none.reason_code == "POLICY_NOT_FOUND"

    # Customer mismatch: FAIL with POLICY_OWNER_MISMATCH
    pol_mismatch = make_valid_policy_input(customer_id="cust-other")
    inp_mismatch = make_valid_validation_input(policy=pol_mismatch)
    out_mismatch = evaluate_validation(inp_mismatch)
    v01_mis = next(r for r in out_mismatch.rule_results if r.rule_id == "V01")
    assert v01_mis.outcome == RuleOutcomeEnum.FAIL
    assert v01_mis.reason_code == "POLICY_OWNER_MISMATCH"


def test_v02_policy_active() -> None:
    # ACTIVE: PASS
    inp = make_valid_validation_input()
    out = evaluate_validation(inp)
    v02 = next(r for r in out.rule_results if r.rule_id == "V02")
    assert v02.outcome == RuleOutcomeEnum.PASS

    # LAPSED / CANCELLED / SUSPENDED: FAIL
    for status_val in ("LAPSED", "CANCELLED", "EXPIRED", "SUSPENDED"):
        pol = make_valid_policy_input(status=status_val)
        inp_fail = make_valid_validation_input(policy=pol)
        out_fail = evaluate_validation(inp_fail)
        v02_fail = next(r for r in out_fail.rule_results if r.rule_id == "V02")
        assert v02_fail.outcome == RuleOutcomeEnum.FAIL
        assert v02_fail.reason_code == "POLICY_NOT_ACTIVE"


def test_v03_incident_in_policy_period() -> None:
    start = date(2026, 1, 1)
    end = date(2026, 12, 31)

    # Exactly on start_date: PASS
    clm_start = make_valid_claim_input(incident_date=start, registered_date=start + timedelta(days=5))
    inp_start = make_valid_validation_input(claim=clm_start, eval_date=start + timedelta(days=5))
    out_start = evaluate_validation(inp_start)
    assert next(r for r in out_start.rule_results if r.rule_id == "V03").outcome == RuleOutcomeEnum.PASS

    # Exactly on end_date: PASS
    clm_end = make_valid_claim_input(incident_date=end, registered_date=end)
    inp_end = make_valid_validation_input(claim=clm_end, eval_date=end)
    out_end = evaluate_validation(inp_end)
    assert next(r for r in out_end.rule_results if r.rule_id == "V03").outcome == RuleOutcomeEnum.PASS

    # Day before start: FAIL
    clm_before = make_valid_claim_input(incident_date=start - timedelta(days=1), registered_date=start)
    inp_before = make_valid_validation_input(claim=clm_before, eval_date=start)
    out_before = evaluate_validation(inp_before)
    v03_before = next(r for r in out_before.rule_results if r.rule_id == "V03")
    assert v03_before.outcome == RuleOutcomeEnum.FAIL
    assert v03_before.reason_code == "INCIDENT_OUTSIDE_POLICY_PERIOD"

    # Day after end: FAIL
    clm_after = make_valid_claim_input(incident_date=end + timedelta(days=1), registered_date=end + timedelta(days=2))
    inp_after = make_valid_validation_input(claim=clm_after, eval_date=end + timedelta(days=2))
    out_after = evaluate_validation(inp_after)
    v03_after = next(r for r in out_after.rule_results if r.rule_id == "V03")
    assert v03_after.outcome == RuleOutcomeEnum.FAIL
    assert v03_after.reason_code == "INCIDENT_OUTSIDE_POLICY_PERIOD"


def test_v04_valid_incident_date() -> None:
    eval_d = date(2026, 5, 1)

    # Future date relative to eval_date: FAIL
    clm_future = make_valid_claim_input(incident_date=date(2026, 5, 2), registered_date=date(2026, 5, 2))
    inp_future = make_valid_validation_input(claim=clm_future, eval_date=eval_d)
    out_future = evaluate_validation(inp_future)
    v04_fut = next(r for r in out_future.rule_results if r.rule_id == "V04")
    assert v04_fut.outcome == RuleOutcomeEnum.FAIL
    assert v04_fut.reason_code == "INCIDENT_DATE_INVALID"

    # Incident date after registered date: FAIL
    clm_after_reg = make_valid_claim_input(incident_date=date(2026, 4, 10), registered_date=date(2026, 4, 5))
    inp_after_reg = make_valid_validation_input(claim=clm_after_reg, eval_date=eval_d)
    out_after_reg = evaluate_validation(inp_after_reg)
    v04_reg = next(r for r in out_after_reg.rule_results if r.rule_id == "V04")
    assert v04_reg.outcome == RuleOutcomeEnum.FAIL


def test_v05_filing_window() -> None:
    inc_date = date(2026, 1, 1)

    # 90 days exact boundary: PASS
    clm_90 = make_valid_claim_input(incident_date=inc_date, registered_date=inc_date + timedelta(days=90))
    inp_90 = make_valid_validation_input(claim=clm_90, eval_date=inc_date + timedelta(days=90))
    out_90 = evaluate_validation(inp_90)
    assert next(r for r in out_90.rule_results if r.rule_id == "V05").outcome == RuleOutcomeEnum.PASS

    # 91 days: FAIL
    clm_91 = make_valid_claim_input(incident_date=inc_date, registered_date=inc_date + timedelta(days=91))
    inp_91 = make_valid_validation_input(claim=clm_91, eval_date=inc_date + timedelta(days=91))
    out_91 = evaluate_validation(inp_91)
    v05_fail = next(r for r in out_91.rule_results if r.rule_id == "V05")
    assert v05_fail.outcome == RuleOutcomeEnum.FAIL
    assert v05_fail.reason_code == "FILING_WINDOW_EXCEEDED"


def test_v07_coverage_exists_and_not_excluded() -> None:
    # No matching coverage for category: FAIL
    inp_no_cov = make_valid_validation_input(coverages=())
    out_no_cov = evaluate_validation(inp_no_cov)
    v07_no = next(r for r in out_no_cov.rule_results if r.rule_id == "V07")
    assert v07_no.outcome == RuleOutcomeEnum.FAIL
    assert v07_no.reason_code == "NO_COVERAGE"

    # Coverage marked is_excluded=True: FAIL
    cov_excl = make_valid_coverage_input(is_excluded=True)
    inp_cov_excl = make_valid_validation_input(coverages=(cov_excl,))
    out_cov_excl = evaluate_validation(inp_cov_excl)
    v07_ex = next(r for r in out_cov_excl.rule_results if r.rule_id == "V07")
    assert v07_ex.outcome == RuleOutcomeEnum.FAIL


def test_v08_policy_exclusion_check() -> None:
    # No exclusion: PASS
    inp = make_valid_validation_input(exclusions=())
    out = evaluate_validation(inp)
    assert next(r for r in out.rule_results if r.rule_id == "V08").outcome == RuleOutcomeEnum.PASS

    # Exclusion code matches claim category: FAIL
    excl = ExclusionInput(exclusion_code="CAT_SURGERY", description="Cosmetic and elective surgeries")
    inp_ex = make_valid_validation_input(exclusions=(excl,))
    out_ex = evaluate_validation(inp_ex)
    v08 = next(r for r in out_ex.rule_results if r.rule_id == "V08")
    assert v08.outcome == RuleOutcomeEnum.FAIL
    assert v08.reason_code == "EXCLUDED"


def test_v09_waiting_period_satisfied() -> None:
    pol_start = date(2026, 1, 1)
    waiting_days = 30
    eligible_date = pol_start + timedelta(days=waiting_days)  # Jan 31

    # Exact boundary (eligible date): PASS
    clm_exact = make_valid_claim_input(incident_date=eligible_date, registered_date=eligible_date + timedelta(days=1))
    inp_exact = make_valid_validation_input(claim=clm_exact, eval_date=eligible_date + timedelta(days=1))
    out_exact = evaluate_validation(inp_exact)
    assert next(r for r in out_exact.rule_results if r.rule_id == "V09").outcome == RuleOutcomeEnum.PASS

    # Day before eligible date: FAIL
    clm_early = make_valid_claim_input(incident_date=eligible_date - timedelta(days=1), registered_date=eligible_date)
    inp_early = make_valid_validation_input(claim=clm_early, eval_date=eligible_date)
    out_early = evaluate_validation(inp_early)
    v09 = next(r for r in out_early.rule_results if r.rule_id == "V09")
    assert v09.outcome == RuleOutcomeEnum.FAIL
    assert v09.reason_code == "WAITING_PERIOD_NOT_MET"


def test_v10_positive_requested_amount() -> None:
    # Positive: PASS
    inp = make_valid_validation_input(claim=make_valid_claim_input(requested_amount=Decimal("0.01")))
    out = evaluate_validation(inp)
    assert next(r for r in out.rule_results if r.rule_id == "V10").outcome == RuleOutcomeEnum.PASS

    # Zero: FAIL
    inp_zero = make_valid_validation_input(claim=make_valid_claim_input(requested_amount=Decimal("0.00")))
    out_zero = evaluate_validation(inp_zero)
    v10_zero = next(r for r in out_zero.rule_results if r.rule_id == "V10")
    assert v10_zero.outcome == RuleOutcomeEnum.FAIL
    assert v10_zero.reason_code == "AMOUNT_INVALID"

    # Negative: FAIL
    inp_neg = make_valid_validation_input(claim=make_valid_claim_input(requested_amount=Decimal("-100.00")))
    out_neg = evaluate_validation(inp_neg)
    v10_neg = next(r for r in out_neg.rule_results if r.rule_id == "V10")
    assert v10_neg.outcome == RuleOutcomeEnum.FAIL


def test_v11_coverage_limit_policy_behaviors() -> None:
    # Remaining limit = 50000 - 45000 = 5000
    cov = make_valid_coverage_input(limit_amount=Decimal("50000.00"), consumed_amount=Decimal("45000.00"))

    # Requesting 6000 under CAP_WITH_WARNING (default): PASS with limit_warning=True in snapshot
    inp_cap = make_valid_validation_input(
        claim=make_valid_claim_input(requested_amount=Decimal("6000.00")),
        coverages=(cov,),
    )
    out_cap = evaluate_validation(inp_cap)
    v11_cap = next(r for r in out_cap.rule_results if r.rule_id == "V11")
    assert v11_cap.outcome == RuleOutcomeEnum.PASS
    assert v11_cap.inputs_snapshot["limit_warning"] is True

    # Requesting 6000 under REJECT policy: FAIL
    custom_ruleset = dict(ACTIVE_RULESET)
    custom_ruleset["claim_limit_policy"] = "REJECT"
    inp_reject = make_valid_validation_input(
        claim=make_valid_claim_input(requested_amount=Decimal("6000.00")),
        coverages=(cov,),
        ruleset=custom_ruleset,
    )
    out_reject = evaluate_validation(inp_reject)
    v11_rej = next(r for r in out_reject.rule_results if r.rule_id == "V11")
    assert v11_rej.outcome == RuleOutcomeEnum.FAIL
    assert v11_rej.reason_code == "LIMIT_EXCEEDED"


def test_v12_mandatory_fields_present() -> None:
    # Missing location
    inp_no_loc = make_valid_validation_input(claim=make_valid_claim_input(location=""))
    out_no_loc = evaluate_validation(inp_no_loc)
    v12 = next(r for r in out_no_loc.rule_results if r.rule_id == "V12")
    assert v12.outcome == RuleOutcomeEnum.FAIL
    assert v12.reason_code == "MISSING_FIELD"
    assert "incident_location" in v12.inputs_snapshot["missing_fields"]

    # Missing description
    inp_no_desc = make_valid_validation_input(claim=make_valid_claim_input(description="   "))
    out_no_desc = evaluate_validation(inp_no_desc)
    assert next(r for r in out_no_desc.rule_results if r.rule_id == "V12").outcome == RuleOutcomeEnum.FAIL


def test_v13_document_lifecycle_and_blocked_docs() -> None:
    # All mandatory docs VALID: PASS
    inp_valid = make_valid_validation_input()
    out_valid = evaluate_validation(inp_valid)
    assert next(r for r in out_valid.rule_results if r.rule_id == "V13").outcome == RuleOutcomeEnum.PASS
    assert out_valid.overall_result == ValidationOutcomeEnum.PASS

    # Mandatory doc missing: V13 FAIL with reason DOCUMENTS_MISSING
    inp_missing = make_valid_validation_input(documents=())
    out_missing = evaluate_validation(inp_missing)
    v13_mis = next(r for r in out_missing.rule_results if r.rule_id == "V13")
    assert v13_mis.outcome == RuleOutcomeEnum.FAIL
    assert v13_mis.reason_code == "DOCUMENTS_MISSING"
    # When V13 is the sole failing rule, overall outcome is BLOCKED_DOCS (Section 9)
    assert out_missing.overall_result == ValidationOutcomeEnum.BLOCKED_DOCS

    # Mandatory doc PENDING_REVIEW: FAIL with DOCUMENT_PENDING_REVIEW
    pending_docs = (
        DocumentInput(document_id="doc-1", doc_code="DOC_BILL", status="PENDING_REVIEW"),
        DocumentInput(document_id="doc-2", doc_code="DOC_REPORT", status="VALID"),
    )
    inp_pending = make_valid_validation_input(documents=pending_docs)
    out_pending = evaluate_validation(inp_pending)
    v13_pen = next(r for r in out_pending.rule_results if r.rule_id == "V13")
    assert v13_pen.outcome == RuleOutcomeEnum.FAIL
    assert v13_pen.reason_code == "DOCUMENT_PENDING_REVIEW"
    assert out_pending.overall_result == ValidationOutcomeEnum.BLOCKED_DOCS

    # Mandatory doc INVALID: FAIL with DOCUMENT_INVALID
    invalid_docs = (
        DocumentInput(document_id="doc-1", doc_code="DOC_BILL", status="INVALID"),
        DocumentInput(document_id="doc-2", doc_code="DOC_REPORT", status="VALID"),
    )
    inp_invalid = make_valid_validation_input(documents=invalid_docs)
    out_invalid = evaluate_validation(inp_invalid)
    v13_inv = next(r for r in out_invalid.rule_results if r.rule_id == "V13")
    assert v13_inv.outcome == RuleOutcomeEnum.FAIL
    assert v13_inv.reason_code == "DOCUMENT_INVALID"
    assert out_invalid.overall_result == ValidationOutcomeEnum.BLOCKED_DOCS


def test_multiple_failures_lead_to_fail_not_blocked_docs() -> None:
    # Both V02 (lapsed policy) and V13 (missing docs) fail -> overall outcome must be FAIL
    pol_lapsed = make_valid_policy_input(status="LAPSED")
    inp = make_valid_validation_input(policy=pol_lapsed, documents=())
    out = evaluate_validation(inp)
    assert out.overall_result == ValidationOutcomeEnum.FAIL


def test_engine_determinism_under_shuffled_inputs() -> None:
    # Shuffle order of coverages, exclusions, documents, and required document types
    base_inp = make_valid_validation_input()
    res1 = evaluate_validation(base_inp)

    for _ in range(10):
        # Create varied lists
        covs = list(base_inp.coverages) + [
            CoverageInput(
                coverage_id=f"cov-{i}",
                claim_category=f"CAT_{i}",
                limit_amount=Decimal("1000.00"),
                consumed_amount=Decimal("0.00"),
                deductible=Decimal("0.00"),
                copay_percent=Decimal("0.00"),
                waiting_period_days=0,
                is_excluded=False,
            )
            for i in range(5)
        ]
        docs = list(base_inp.documents)
        random.shuffle(covs)
        random.shuffle(docs)

        shuffled_inp = ValidationInput(
            claim=base_inp.claim,
            policy=base_inp.policy,
            coverages=tuple(covs),
            exclusions=base_inp.exclusions,
            documents=tuple(docs),
            required_document_types=base_inp.required_document_types,
            evaluation_date=base_inp.evaluation_date,
            ruleset=base_inp.ruleset,
        )

        res_shuffled = evaluate_validation(shuffled_inp)
        assert res_shuffled.overall_result == res1.overall_result
        assert len(res_shuffled.rule_results) == len(res1.rule_results)
        for r_orig, r_shuf in zip(
            res1.rule_results, res_shuffled.rule_results, strict=True
        ):
            assert r_orig.rule_id == r_shuf.rule_id
            assert r_orig.outcome == r_shuf.outcome
            assert r_orig.reason_code == r_shuf.reason_code


# ========================================================
# Hypothesis Property-Based Testing
# ========================================================

@given(
    amount=st.decimals(min_value=-100000, max_value=100000, places=2),
    days_offset=st.integers(min_value=-365, max_value=365),
)
def test_hypothesis_engine_invariants(amount: Decimal, days_offset: int) -> None:
    eval_date = date(2026, 6, 1)
    inc_date = eval_date + timedelta(days=days_offset)
    reg_date = eval_date

    claim = ClaimInput(
        claim_id="hyp-clm-1",
        claim_ref="CLM-HYP",
        customer_id="cust-1",
        policy_id="pol-1",
        claim_category="CAT_SURGERY",
        incident_date=inc_date,
        incident_location="Hospital",
        incident_description="Test",
        requested_amount=amount,
        registered_date=reg_date,
    )

    inp = make_valid_validation_input(claim=claim, eval_date=eval_date)
    out = evaluate_validation(inp)

    # Invariant 1: overall_result is strictly one of the 3 legal outcomes
    assert out.overall_result in (
        ValidationOutcomeEnum.PASS,
        ValidationOutcomeEnum.FAIL,
        ValidationOutcomeEnum.BLOCKED_DOCS,
    )

    # Invariant 2: every rule result is valid enum
    for r in out.rule_results:
        assert r.outcome in (RuleOutcomeEnum.PASS, RuleOutcomeEnum.FAIL, RuleOutcomeEnum.SKIPPED)

    # Invariant 3: if amount <= 0, V10 MUST be FAIL
    v10 = next(r for r in out.rule_results if r.rule_id == "V10")
    if amount <= Decimal("0.00"):
        assert v10.outcome == RuleOutcomeEnum.FAIL
    else:
        assert v10.outcome == RuleOutcomeEnum.PASS

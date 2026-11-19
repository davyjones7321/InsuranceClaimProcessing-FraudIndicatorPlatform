"""Comprehensive unit, property, and determinism tests for Risk Indicator Engine (R01-R10)."""

import random
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from app.engine.risk import evaluate_risk
from app.engine.types import (
    ClaimInput,
    CoverageInput,
    DocumentInput,
    HistoricalClaimInput,
    PolicyInput,
    RiskCategoryEnum,
    RiskInput,
)
from app.rules.loader import ACTIVE_RULESET

_DEFAULT_SENTINEL = object()


def make_clean_claim_input(
    incident_date: date = date(2026, 6, 15),
    registered_date: date = date(2026, 6, 20),
    requested_amount: Decimal = Decimal("12345.67"),
    claim_category: str = "CAT_HEALTH_OPD",
) -> ClaimInput:
    return ClaimInput(
        claim_id="clm-clean-01",
        claim_ref="CLM-2026-100001",
        customer_id="cust-001",
        policy_id="pol-001",
        claim_category=claim_category,
        incident_date=incident_date,
        incident_location="Hospital, CA",
        incident_description="Standard treatment consultation",
        requested_amount=requested_amount,
        registered_date=registered_date,
    )


def make_clean_policy_input(
    start_date: date = date(2026, 1, 1),
    end_date: date = date(2026, 12, 31),
) -> PolicyInput:
    return PolicyInput(
        policy_id="pol-001",
        policy_no="POL-CLEAN-01",
        customer_id="cust-001",
        product_type="HEALTH",
        start_date=start_date,
        end_date=end_date,
        status="ACTIVE",
    )


def make_clean_coverage_input(
    limit_amount: Decimal = Decimal("100000.00"),
    consumed_amount: Decimal = Decimal("0.00"),
) -> CoverageInput:
    return CoverageInput(
        coverage_id="cov-001",
        claim_category="CAT_HEALTH_OPD",
        limit_amount=limit_amount,
        consumed_amount=consumed_amount,
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
    )


def make_risk_input(
    claim: ClaimInput | None = None,
    policy: Any = _DEFAULT_SENTINEL,
    coverage: Any = _DEFAULT_SENTINEL,
    customer_name: str = "Alice Smith",
    customer_state_region: str | None = "CA",
    documents: tuple[DocumentInput, ...] = (),
    historical_claims: tuple[HistoricalClaimInput, ...] = (),
    peer_approved_amounts: tuple[Decimal, ...] = (),
    eval_date: date = date(2026, 6, 20),
    ruleset: dict[str, Any] | None = None,
) -> RiskInput:
    resolved_policy = make_clean_policy_input() if policy is _DEFAULT_SENTINEL else policy
    resolved_coverage = make_clean_coverage_input() if coverage is _DEFAULT_SENTINEL else coverage
    return RiskInput(
        claim=claim or make_clean_claim_input(),
        customer_name=customer_name,
        customer_state_region=customer_state_region,
        policy=resolved_policy,
        coverage=resolved_coverage,
        documents=documents,
        historical_claims=historical_claims,
        peer_approved_amounts=peer_approved_amounts,
        evaluation_date=eval_date,
        ruleset=ruleset or ACTIVE_RULESET,
    )


# ========================================================
# Unit Tests for Individual Risk Indicators R01 - R10
# ========================================================


def test_r01_repeated_claims_by_customer() -> None:
    eval_d = date(2026, 6, 20)

    # 2 prior claims: below threshold (3) -> Untriggered
    history_2 = tuple(
        HistoricalClaimInput(
            claim_id=f"hc-{i}",
            claim_ref=f"CLM-HIST-{i}",
            customer_id="cust-001",
            policy_id="pol-other",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 1, 1),
            registered_date=date(2026, 1, 10),
            requested_amount=Decimal("1000.00"),
            status="VALIDATED",
        )
        for i in range(2)
    )
    inp_2 = make_risk_input(historical_claims=history_2, eval_date=eval_d)
    out_2 = evaluate_risk(inp_2)
    r01_2 = next(r for r in out_2.indicator_results if r.indicator_id == "R01")
    assert not r01_2.triggered
    assert r01_2.weight_applied == 0

    # 3 prior claims within 365 days -> Triggered (weight 20)
    history_3 = history_2 + (
        HistoricalClaimInput(
            claim_id="hc-3",
            claim_ref="CLM-HIST-3",
            customer_id="cust-001",
            policy_id="pol-other",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 2, 1),
            registered_date=date(2026, 2, 10),
            requested_amount=Decimal("1000.00"),
            status="VALIDATED",
        ),
    )
    inp_3 = make_risk_input(historical_claims=history_3, eval_date=eval_d)
    out_3 = evaluate_risk(inp_3)
    r01_3 = next(r for r in out_3.indicator_results if r.indicator_id == "R01")
    assert r01_3.triggered
    assert r01_3.weight_applied == 20

    # Prior claim WITHDRAWN -> Ignored from count
    history_withdrawn = history_2 + (
        HistoricalClaimInput(
            claim_id="hc-w",
            claim_ref="CLM-HIST-W",
            customer_id="cust-001",
            policy_id="pol-other",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 2, 1),
            registered_date=date(2026, 2, 10),
            requested_amount=Decimal("1000.00"),
            status="WITHDRAWN",
        ),
    )
    inp_w = make_risk_input(historical_claims=history_withdrawn, eval_date=eval_d)
    out_w = evaluate_risk(inp_w)
    r01_w = next(r for r in out_w.indicator_results if r.indicator_id == "R01")
    assert not r01_w.triggered


def test_r02_repeated_claims_on_policy() -> None:
    eval_d = date(2026, 6, 20)

    # 1 claim on policy: below threshold (2) -> Untriggered
    history_1 = (
        HistoricalClaimInput(
            claim_id="pol-c1",
            claim_ref="CLM-POL-1",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 5, 1),
            registered_date=date(2026, 5, 5),
            requested_amount=Decimal("1000.00"),
            status="VALIDATED",
        ),
    )
    inp_1 = make_risk_input(historical_claims=history_1, eval_date=eval_d)
    r02_1 = next(r for r in evaluate_risk(inp_1).indicator_results if r.indicator_id == "R02")
    assert not r02_1.triggered

    # 2 claims on policy in last 180 days -> Triggered (weight 15)
    history_2 = history_1 + (
        HistoricalClaimInput(
            claim_id="pol-c2",
            claim_ref="CLM-POL-2",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 5, 15),
            registered_date=date(2026, 5, 20),
            requested_amount=Decimal("1000.00"),
            status="VALIDATED",
        ),
    )
    inp_2 = make_risk_input(historical_claims=history_2, eval_date=eval_d)
    r02_2 = next(r for r in evaluate_risk(inp_2).indicator_results if r.indicator_id == "R02")
    assert r02_2.triggered
    assert r02_2.weight_applied == 15


def test_r03_high_amount_vs_limit() -> None:
    # Limit: 100,000. Consumed: 0. Remaining: 100,000.
    # 80% threshold = 80,000.
    # Requesting 80,000: Untriggered (not > 80%)
    inp_80 = make_risk_input(claim=make_clean_claim_input(requested_amount=Decimal("80000.00")))
    r03_80 = next(r for r in evaluate_risk(inp_80).indicator_results if r.indicator_id == "R03")
    assert not r03_80.triggered

    # Requesting 80,000.01: Triggered (weight 20)
    inp_81 = make_risk_input(claim=make_clean_claim_input(requested_amount=Decimal("80000.01")))
    r03_81 = next(r for r in evaluate_risk(inp_81).indicator_results if r.indicator_id == "R03")
    assert r03_81.triggered
    assert r03_81.weight_applied == 20

    # Coverage None -> Skipped
    inp_none = make_risk_input(coverage=None)
    r03_none = next(r for r in evaluate_risk(inp_none).indicator_results if r.indicator_id == "R03")
    assert r03_none.skipped
    assert not r03_none.triggered


def test_r04_unusual_amount_vs_peers() -> None:
    # 9 historical peer claims: below minimum (10) -> Skipped
    peers_9 = tuple(Decimal("1000.00") for _ in range(9))
    inp_9 = make_risk_input(
        claim=make_clean_claim_input(requested_amount=Decimal("50000.00")),
        peer_approved_amounts=peers_9,
    )
    r04_9 = next(r for r in evaluate_risk(inp_9).indicator_results if r.indicator_id == "R04")
    assert r04_9.skipped
    assert not r04_9.triggered
    assert "Insufficient peer claims" in (r04_9.skip_reason or "")

    # 10 historical peer claims with median 10,000. 3x threshold = 30,000.
    peers_10 = tuple(Decimal("10000.00") for _ in range(10))
    # Requesting 30,000: Untriggered
    inp_norm = make_risk_input(
        claim=make_clean_claim_input(requested_amount=Decimal("30000.00")),
        peer_approved_amounts=peers_10,
    )
    r04_norm = next(r for r in evaluate_risk(inp_norm).indicator_results if r.indicator_id == "R04")
    assert not r04_norm.triggered

    # Requesting 30,001: Triggered (weight 15)
    inp_high = make_risk_input(
        claim=make_clean_claim_input(requested_amount=Decimal("30001.00")),
        peer_approved_amounts=peers_10,
    )
    r04_high = next(r for r in evaluate_risk(inp_high).indicator_results if r.indicator_id == "R04")
    assert r04_high.triggered
    assert r04_high.weight_applied == 15


def test_r05_early_claim_window() -> None:
    pol_start = date(2026, 1, 1)

    # 30 days after policy start (Jan 31): Triggered (weight 25)
    clm_30 = make_clean_claim_input(incident_date=pol_start + timedelta(days=30))
    inp_30 = make_risk_input(claim=clm_30)
    r05_30 = next(r for r in evaluate_risk(inp_30).indicator_results if r.indicator_id == "R05")
    assert r05_30.triggered
    assert r05_30.weight_applied == 25

    # 31 days after policy start (Feb 1): Untriggered
    clm_31 = make_clean_claim_input(incident_date=pol_start + timedelta(days=31))
    inp_31 = make_risk_input(claim=clm_31)
    r05_31 = next(r for r in evaluate_risk(inp_31).indicator_results if r.indicator_id == "R05")
    assert not r05_31.triggered


def test_r06_late_in_policy_claim() -> None:
    pol_end = date(2026, 12, 31)

    # 7 days before policy end (Dec 24): Triggered (weight 10)
    clm_7 = make_clean_claim_input(incident_date=pol_end - timedelta(days=7))
    inp_7 = make_risk_input(claim=clm_7)
    r06_7 = next(r for r in evaluate_risk(inp_7).indicator_results if r.indicator_id == "R06")
    assert r06_7.triggered
    assert r06_7.weight_applied == 10

    # 8 days before policy end (Dec 23): Untriggered
    clm_8 = make_clean_claim_input(incident_date=pol_end - timedelta(days=8))
    inp_8 = make_risk_input(claim=clm_8)
    r06_8 = next(r for r in evaluate_risk(inp_8).indicator_results if r.indicator_id == "R06")
    assert not r06_8.triggered


def test_r07_duplicate_suspicion() -> None:
    inc_date = date(2026, 5, 10)
    current_claim = make_clean_claim_input(
        incident_date=inc_date, requested_amount=Decimal("10000.00")
    )

    # Prior claim on same policy, same date, amount 10,400 (4% difference, <= 5%) -> Triggered (weight 30)
    hist_dup = (
        HistoricalClaimInput(
            claim_id="dup-1",
            claim_ref="CLM-DUP-1",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=inc_date,
            registered_date=inc_date + timedelta(days=1),
            requested_amount=Decimal("10400.00"),
            status="VALIDATED",
        ),
    )
    inp_dup = make_risk_input(claim=current_claim, historical_claims=hist_dup)
    r07_dup = next(r for r in evaluate_risk(inp_dup).indicator_results if r.indicator_id == "R07")
    assert r07_dup.triggered
    assert r07_dup.weight_applied == 30

    # Prior claim with 6% difference (10,600) -> Untriggered
    hist_diff = (
        HistoricalClaimInput(
            claim_id="diff-1",
            claim_ref="CLM-DIFF-1",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=inc_date,
            registered_date=inc_date + timedelta(days=1),
            requested_amount=Decimal("10600.00"),
            status="VALIDATED",
        ),
    )
    inp_diff = make_risk_input(claim=current_claim, historical_claims=hist_diff)
    r07_diff = next(r for r in evaluate_risk(inp_diff).indicator_results if r.indicator_id == "R07")
    assert not r07_diff.triggered


def test_r08_inconsistent_document_data() -> None:
    # Customer name: Alice Smith
    # Document with named_person = "Bob Johnson" on VALID doc -> Triggered (weight 20)
    mismatch_docs = (
        DocumentInput(
            document_id="doc-1",
            doc_code="DOC_MED",
            status="VALID",
            named_person="Bob Johnson",
            document_date=date(2026, 6, 15),
        ),
    )
    inp_mismatch = make_risk_input(customer_name="Alice Smith", documents=mismatch_docs)
    r08_mis = next(
        r for r in evaluate_risk(inp_mismatch).indicator_results if r.indicator_id == "R08"
    )
    assert r08_mis.triggered
    assert r08_mis.weight_applied == 20

    # Matching name with normalization ("alice smith" vs "Alice Smith.") -> Untriggered
    match_docs = (
        DocumentInput(
            document_id="doc-2",
            doc_code="DOC_MED",
            status="VALID",
            named_person="alice smith",
            document_date=date(2026, 6, 15),
        ),
    )
    inp_match = make_risk_input(customer_name="Alice Smith", documents=match_docs)
    r08_mat = next(r for r in evaluate_risk(inp_match).indicator_results if r.indicator_id == "R08")
    assert not r08_mat.triggered

    # Document marked INVALID -> Name mismatch ignored
    invalid_docs = (
        DocumentInput(
            document_id="doc-3",
            doc_code="DOC_MED",
            status="INVALID",
            named_person="Different Person",
            document_date=date(2026, 6, 15),
        ),
    )
    inp_inv = make_risk_input(customer_name="Alice Smith", documents=invalid_docs)
    assert not next(
        r for r in evaluate_risk(inp_inv).indicator_results if r.indicator_id == "R08"
    ).triggered


def test_r09_late_filing() -> None:
    inc_date = date(2026, 5, 1)

    # 15 days: Untriggered
    clm_15 = make_clean_claim_input(
        incident_date=inc_date, registered_date=inc_date + timedelta(days=15)
    )
    inp_15 = make_risk_input(claim=clm_15)
    r09_15 = next(r for r in evaluate_risk(inp_15).indicator_results if r.indicator_id == "R09")
    assert not r09_15.triggered

    # 16 days: Triggered (weight 5)
    clm_16 = make_clean_claim_input(
        incident_date=inc_date, registered_date=inc_date + timedelta(days=16)
    )
    inp_16 = make_risk_input(claim=clm_16)
    r09_16 = next(r for r in evaluate_risk(inp_16).indicator_results if r.indicator_id == "R09")
    assert r09_16.triggered
    assert r09_16.weight_applied == 5


def test_r10_round_number_amount() -> None:
    # 40,000 (multiple of 10,000, but < 50,000) -> Untriggered
    inp_40k = make_risk_input(claim=make_clean_claim_input(requested_amount=Decimal("40000.00")))
    assert not next(
        r for r in evaluate_risk(inp_40k).indicator_results if r.indicator_id == "R10"
    ).triggered

    # 55,000 (>= 50,000, but not multiple of 10,000) -> Untriggered
    inp_55k = make_risk_input(claim=make_clean_claim_input(requested_amount=Decimal("55000.00")))
    assert not next(
        r for r in evaluate_risk(inp_55k).indicator_results if r.indicator_id == "R10"
    ).triggered

    # 50,000 (exact boundary >= 50,000 and multiple of 10,000) -> Triggered (weight 5)
    inp_50k = make_risk_input(claim=make_clean_claim_input(requested_amount=Decimal("50000.00")))
    r10_50k = next(r for r in evaluate_risk(inp_50k).indicator_results if r.indicator_id == "R10")
    assert r10_50k.triggered
    assert r10_50k.weight_applied == 5


# ========================================================
# Score Aggregation, Categories, and Invariance Tests
# ========================================================


def test_risk_score_categories_and_routing() -> None:
    # Clean claim: score 0 -> LOW, requires_investigation = False
    clean_inp = make_risk_input()
    clean_out = evaluate_risk(clean_inp)
    assert clean_out.score == 0
    assert clean_out.category == RiskCategoryEnum.LOW
    assert not clean_out.requires_investigation

    # Trigger R05 (weight 25) -> Score 25 -> MEDIUM, requires_investigation = True
    early_claim = make_clean_claim_input(
        incident_date=date(2026, 1, 10), registered_date=date(2026, 1, 15)
    )
    inp_med = make_risk_input(claim=early_claim)
    out_med = evaluate_risk(inp_med)
    assert out_med.score == 25
    assert out_med.category == RiskCategoryEnum.MEDIUM
    assert out_med.requires_investigation

    # Trigger R05 (25) + R07 (30) -> Score 55 -> HIGH, requires_investigation = True
    hist_dup = (
        HistoricalClaimInput(
            claim_id="dup-1",
            claim_ref="CLM-DUP",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 1, 10),
            registered_date=date(2026, 1, 15),
            requested_amount=early_claim.requested_amount,
            status="VALIDATED",
        ),
    )
    inp_high = make_risk_input(claim=early_claim, historical_claims=hist_dup)
    out_high = evaluate_risk(inp_high)
    assert out_high.score == 55
    assert out_high.category == RiskCategoryEnum.HIGH
    assert out_high.requires_investigation


def test_risk_score_capped_at_100() -> None:
    # Combine indicators totaling > 100 points
    # R05 (25) + R07 (30) + R01 (20) + R02 (15) + R03 (20) = 110 points
    early_claim = make_clean_claim_input(
        incident_date=date(2026, 1, 10),
        requested_amount=Decimal("90000.00"),  # Triggers R03 (>80%)
    )
    # 3 prior claims on same customer and policy
    history = tuple(
        HistoricalClaimInput(
            claim_id=f"h-{i}",
            claim_ref=f"CLM-H-{i}",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 1, 10),
            registered_date=date(2026, 1, 15),
            requested_amount=Decimal("90000.00"),  # Triggers R07 duplicate
            status="VALIDATED",
        )
        for i in range(3)
    )
    inp = make_risk_input(claim=early_claim, historical_claims=history)
    out = evaluate_risk(inp)
    assert out.score == 100


def test_risk_engine_determinism_under_shuffled_inputs() -> None:
    base_inp = make_risk_input(
        claim=make_clean_claim_input(incident_date=date(2026, 1, 10)),
        historical_claims=tuple(
            HistoricalClaimInput(
                claim_id=f"hc-{i}",
                claim_ref=f"CLM-{i}",
                customer_id="cust-001",
                policy_id="pol-001",
                claim_category="CAT_HEALTH_OPD",
                incident_date=date(2026, 1, i + 1),
                registered_date=date(2026, 1, i + 2),
                requested_amount=Decimal(f"{1000 * (i + 1)}.00"),
                status="VALIDATED",
            )
            for i in range(8)
        ),
        peer_approved_amounts=tuple(Decimal(f"{2000 * (i + 1)}.00") for i in range(12)),
    )
    base_out = evaluate_risk(base_inp)

    for _ in range(10):
        h_shuf = list(base_inp.historical_claims)
        p_shuf = list(base_inp.peer_approved_amounts)
        random.shuffle(h_shuf)
        random.shuffle(p_shuf)

        shuffled_inp = RiskInput(
            claim=base_inp.claim,
            customer_name=base_inp.customer_name,
            customer_state_region=base_inp.customer_state_region,
            policy=base_inp.policy,
            coverage=base_inp.coverage,
            documents=base_inp.documents,
            historical_claims=tuple(h_shuf),
            peer_approved_amounts=tuple(p_shuf),
            evaluation_date=base_inp.evaluation_date,
            ruleset=base_inp.ruleset,
        )

        shuf_out = evaluate_risk(shuffled_inp)
        assert shuf_out.score == base_out.score
        assert shuf_out.category == base_out.category
        assert shuf_out.requires_investigation == base_out.requires_investigation

        for r_orig, r_shuf in zip(
            base_out.indicator_results, shuf_out.indicator_results, strict=True
        ):
            assert r_orig.indicator_id == r_shuf.indicator_id
            assert r_orig.triggered == r_shuf.triggered
            assert r_orig.weight_applied == r_shuf.weight_applied


# ========================================================
# Hypothesis Property-Based Testing
# ========================================================


@given(
    amount=st.decimals(min_value=1, max_value=500000, places=2),
    days_offset=st.integers(min_value=0, max_value=360),
)
def test_hypothesis_risk_engine_invariants(amount: Decimal, days_offset: int) -> None:
    pol_start = date(2026, 1, 1)
    inc_date = pol_start + timedelta(days=days_offset)
    reg_date = inc_date + timedelta(days=5)

    claim = ClaimInput(
        claim_id="hyp-clm",
        claim_ref="CLM-HYP",
        customer_id="cust-hyp",
        policy_id="pol-hyp",
        claim_category="CAT_HEALTH_OPD",
        incident_date=inc_date,
        incident_location="Hospital",
        incident_description="Hypothesis test",
        requested_amount=amount,
        registered_date=reg_date,
    )

    inp = make_risk_input(claim=claim, eval_date=reg_date)
    out = evaluate_risk(inp)

    # Invariant 1: Score is strictly bounded between 0 and 100
    assert 0 <= out.score <= 100

    # Invariant 2: Category matches strict range boundaries
    if out.score <= 24:
        assert out.category == RiskCategoryEnum.LOW
        assert not out.requires_investigation
    elif out.score <= 49:
        assert out.category == RiskCategoryEnum.MEDIUM
        assert out.requires_investigation
    else:
        assert out.category == RiskCategoryEnum.HIGH
        assert out.requires_investigation

    # Invariant 3: Score equals sum of triggered weights capped at 100
    expected_sum = min(100, sum(r.weight_applied for r in out.indicator_results if r.triggered))
    assert out.score == expected_sum

    # Invariant 4: Exactly 10 indicators present in sorted order R01 to R10
    ind_ids = [r.indicator_id for r in out.indicator_results]
    assert ind_ids == [f"R{i:02d}" for i in range(1, 11)]

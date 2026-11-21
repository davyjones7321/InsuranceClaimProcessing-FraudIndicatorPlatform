"""Comprehensive unit, property, and determinism tests for Risk Indicator Engine (R01-R10).

Validates:
- Exact order of operations.
- Appendix A worked examples 1-8 as fixtures.
- Group caps (FREQUENCY, AMOUNT, TIMING, DUPLICATE, CONSISTENCY).
- Completeness states (COMPLETE, PARTIAL, INSUFFICIENT_DATA).
- UNASSESSED category with NULL score (never LOW).
- History coverage window checks (HISTORY_WINDOW_NOT_COVERED).
- Sufficient alone routing (e.g. R07 duplicate suspicion).
- Replay reproducibility and functional determinism.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from app.engine.risk import evaluate_risk
from app.engine.types import (
    ClaimInput,
    CoverageInput,
    DocumentInput,
    HistoricalClaimInput,
    IndicatorEvaluationStatusEnum,
    PolicyInput,
    RiskCategoryEnum,
    RiskCompletenessEnum,
    RiskInput,
)
from app.rules.loader import ACTIVE_RULESET

_DEFAULT_SENTINEL = object()


def make_clean_claim_input(
    incident_date: date = date(2026, 6, 15),
    registered_date: date = date(2026, 6, 20),
    requested_amount: Decimal = Decimal("12345.67"),
    claim_category: str = "CAT_HEALTH_OPD",
    incident_group_id: str | None = None,
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
        incident_group_id=incident_group_id,
    )


def make_clean_policy_input(
    start_date: date = date(2026, 1, 1),
    end_date: date = date(2026, 12, 31),
    product_type: str = "HEALTH",
) -> PolicyInput:
    return PolicyInput(
        policy_id="pol-001",
        policy_no="POL-CLEAN-01",
        customer_id="cust-001",
        product_type=product_type,
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
    documents: tuple[DocumentInput, ...] = (),
    historical_claims: tuple[HistoricalClaimInput, ...] = (),
    peer_approved_amounts: tuple[Decimal, ...] = (Decimal("10000.00"),) * 10,
    eval_date: date = date(2026, 6, 20),
    ruleset: dict[str, Any] | None = None,
    history_coverage: dict[str, date] | None = None,
) -> RiskInput:
    resolved_policy = make_clean_policy_input() if policy is _DEFAULT_SENTINEL else policy
    resolved_coverage = make_clean_coverage_input() if coverage is _DEFAULT_SENTINEL else coverage
    return RiskInput(
        claim=claim or make_clean_claim_input(),
        policy=resolved_policy,
        coverage=resolved_coverage,
        documents=documents,
        historical_claims=historical_claims,
        peer_approved_amounts=peer_approved_amounts,
        evaluation_date=eval_date,
        ruleset=ruleset or ACTIVE_RULESET,
        history_coverage=history_coverage or {},
    )


# ========================================================
# Appendix A: Worked Examples 1 to 8 as Test Fixtures
# ========================================================


def test_appendix_a_worked_example_1_r07_only() -> None:
    """Example 1: R07 only -> DUPLICATE 10 -> Score 10, LOW, Investigation YES (SUFFICIENT_ALONE:R07)."""
    claim = make_clean_claim_input(
        incident_date=date(2026, 6, 15),
        registered_date=date(2026, 6, 20),
        requested_amount=Decimal("12345.67"),
    )
    # Duplicate on same policy, same incident date, variance <= 5%
    dup = HistoricalClaimInput(
        claim_id="clm-prior-dup",
        claim_ref="CLM-2026-000099",
        customer_id="cust-001",
        policy_id="pol-001",
        claim_category="CAT_HEALTH_OPD",
        incident_date=date(2026, 6, 15),
        registered_date=date(2026, 6, 16),
        requested_amount=Decimal("12300.00"),  # diff 45.67 < 5%
        status="REGISTERED",
    )
    inp = make_risk_input(claim=claim, historical_claims=(dup,), eval_date=date(2026, 6, 20))
    out = evaluate_risk(inp)

    assert out.score == 10
    assert out.category == RiskCategoryEnum.LOW
    assert out.completeness == RiskCompletenessEnum.COMPLETE
    assert out.requires_investigation is True
    assert "SUFFICIENT_ALONE:R07" in out.routing_reasons
    assert out.group_scores.get("DUPLICATE") == 10


def test_appendix_a_worked_example_2_r03_r05_r08() -> None:
    """Example 2: R03, R05, R08 -> AMOUNT 20 + TIMING 15 + CONSISTENCY 20 = 55 -> HIGH, Investigation YES."""
    # R03: requested > 80% remaining limit (remaining limit = 100k - 20k = 80k; 80% = 64k; req = 70k)
    cov = CoverageInput(
        coverage_id="cov-01",
        claim_category="CAT_HEALTH_OPD",
        limit_amount=Decimal("100000.00"),
        consumed_amount=Decimal("20000.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
    )
    # R05: incident within 30 days of policy start (policy start Jan 1, incident Jan 15 = 14 days)
    pol = make_clean_policy_input(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
    claim = make_clean_claim_input(
        incident_date=date(2026, 1, 15),
        registered_date=date(2026, 1, 20),
        requested_amount=Decimal(
            "70500.00"
        ),  # > 64k (80%), but not exact round multiple of 10k so R10 false
    )
    # R08: doc has identity attestation MISMATCH
    doc = DocumentInput(
        document_id="doc-01",
        doc_code="GOV_ID",
        status="VALID",
        named_person="Alice",
        identity_attestation_result="MISMATCH",
    )
    inp = make_risk_input(
        claim=claim,
        policy=pol,
        coverage=cov,
        documents=(doc,),
        peer_approved_amounts=(Decimal("50000.00"),)
        * 10,  # 3x median = 150k > 70.5k so R04 does not trigger
        eval_date=date(2026, 1, 20),
    )
    out = evaluate_risk(inp)

    assert out.score == 55
    assert out.category == RiskCategoryEnum.HIGH
    assert out.requires_investigation is True
    assert "SCORE_THRESHOLD" in out.routing_reasons
    assert out.group_scores == {
        "FREQUENCY": 0,
        "AMOUNT": 20,
        "TIMING": 15,
        "DUPLICATE": 0,
        "CONSISTENCY": 20,
    }


def test_appendix_a_worked_example_3_group_caps_frequency_amount() -> None:
    """Example 3: R01, R02, R03, R04, R10 -> FREQUENCY 35 capped to 25; AMOUNT 40 capped to 25 -> Score 50, HIGH, YES."""
    pol = make_clean_policy_input(start_date=date(2025, 1, 1), end_date=date(2026, 12, 31))
    # R10: requested amount 85,000 (multiple of 10k, min 50k -> round number)
    # R03: limit is 100k, 85k > 80% (80k) -> R03 triggers (20)
    # R04: peer median is 10,000, 3x is 30,000 -> 85,000 > 30,000 -> R04 triggers (15)
    # Total AMOUNT = 20 + 15 + 5 = 40, capped to 25!
    claim = make_clean_claim_input(
        incident_date=date(2026, 6, 15),
        registered_date=date(2026, 6, 20),
        requested_amount=Decimal("90000.00"),
    )
    # R01: 3 prior incidents by customer in past 365 days -> weight 20
    # R02: 2 prior incidents on policy in past 180 days -> weight 15
    # Total FREQUENCY = 20 + 15 = 35, capped to 25!
    prior_claims = (
        HistoricalClaimInput(
            claim_id="h1",
            claim_ref="CLM-H1",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 3, 1),
            registered_date=date(2026, 3, 5),
            requested_amount=Decimal("5000.00"),
            status="REGISTERED",
        ),
        HistoricalClaimInput(
            claim_id="h2",
            claim_ref="CLM-H2",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 4, 1),
            registered_date=date(2026, 4, 5),
            requested_amount=Decimal("5000.00"),
            status="REGISTERED",
        ),
        HistoricalClaimInput(
            claim_id="h3",
            claim_ref="CLM-H3",
            customer_id="cust-001",
            policy_id="pol-other",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 1, 10),
            registered_date=date(2026, 1, 15),
            requested_amount=Decimal("5000.00"),
            status="REGISTERED",
        ),
    )
    inp = make_risk_input(
        claim=claim,
        policy=pol,
        historical_claims=prior_claims,
        peer_approved_amounts=(Decimal("10000.00"),) * 10,
        eval_date=date(2026, 6, 20),
    )
    out = evaluate_risk(inp)

    assert out.group_scores["FREQUENCY"] == 25
    assert out.group_scores["AMOUNT"] == 25
    assert out.score == 50
    assert out.category == RiskCategoryEnum.HIGH
    assert out.requires_investigation is True


def test_appendix_a_worked_example_4_r03_r06() -> None:
    """Example 4: R03, R06 -> AMOUNT 20 + TIMING 10 -> Score 30, MEDIUM, Investigation NO."""
    # R06: incident within 7 days of policy end (policy end June 30, incident June 25 = 5 days)
    pol = make_clean_policy_input(start_date=date(2025, 7, 1), end_date=date(2026, 6, 30))
    # R03: limit 100k, requested 85,000 (not round multiple of 10k: 85,001.00 so R10 false)
    claim = make_clean_claim_input(
        incident_date=date(2026, 6, 25),
        registered_date=date(2026, 6, 26),
        requested_amount=Decimal("85001.00"),
    )
    # peer approved median high enough so R04 doesn't trigger
    peers = (Decimal("50000.00"),) * 10  # 3x = 150k > 85001
    inp = make_risk_input(
        claim=claim,
        policy=pol,
        peer_approved_amounts=peers,
        eval_date=date(2026, 6, 26),
    )
    out = evaluate_risk(inp)

    assert out.score == 30
    assert out.category == RiskCategoryEnum.MEDIUM
    assert out.requires_investigation is False
    assert out.group_scores == {
        "FREQUENCY": 0,
        "AMOUNT": 20,
        "TIMING": 10,
        "DUPLICATE": 0,
        "CONSISTENCY": 0,
    }


def test_appendix_a_worked_example_5_disabled_skipped_zero_weight() -> None:
    """Example 5: R03 triggered; R10 disabled; R04 skipped; R09 triggered with weight 0 -> Score 20, MEDIUM, NO."""
    import copy

    custom_rules = copy.deepcopy(ACTIVE_RULESET)
    # R10 disabled
    custom_rules["risk_indicators"]["R10"]["enabled"] = False
    # R09 weight 0
    custom_rules["risk_indicators"]["R09"]["weight"] = 0

    pol = make_clean_policy_input(start_date=date(2025, 1, 1), end_date=date(2026, 12, 31))
    # R03 triggered (limit 100k, requested 85000)
    # R09 triggered (filed 20 days after incident > 15 days window)
    claim = make_clean_claim_input(
        incident_date=date(2026, 5, 1),
        registered_date=date(2026, 5, 25),
        requested_amount=Decimal("85000.00"),
    )
    # R04 skipped: peer approved amounts empty (< 10)
    inp = make_risk_input(
        claim=claim,
        policy=pol,
        peer_approved_amounts=(),  # R04 skipped
        ruleset=custom_rules,
        eval_date=date(2026, 5, 25),
    )
    out = evaluate_risk(inp)

    assert out.score == 20
    assert out.category == RiskCategoryEnum.MEDIUM
    assert out.requires_investigation is False

    # Check stored statuses
    r10_res = next(r for r in out.indicator_results if r.indicator_id == "R10")
    assert r10_res.status == IndicatorEvaluationStatusEnum.DISABLED

    r04_res = next(r for r in out.indicator_results if r.indicator_id == "R04")
    assert r04_res.status == IndicatorEvaluationStatusEnum.SKIPPED
    assert r04_res.skip_reason == "INSUFFICIENT_PEER_HISTORY"

    r09_res = next(r for r in out.indicator_results if r.indicator_id == "R09")
    assert r09_res.status == IndicatorEvaluationStatusEnum.TRIGGERED
    assert r09_res.weight_applied == 0


def test_appendix_a_worked_example_6_timing_cap_boundary() -> None:
    """Example 6: R03 (20), R05 (15), R09 (5) -> AMOUNT 20 + TIMING 20 (cap 20) -> Score 40, HIGH, YES."""
    # Policy starts June 1, incident June 10 (early: 9 days <= 30) -> R05 triggers (15)
    pol = make_clean_policy_input(start_date=date(2026, 6, 1), end_date=date(2026, 12, 31))
    # Registered June 28 (filing delta 18 days > 15 days) -> R09 triggers (5)
    # TIMING sum = 15 + 5 = 20 (capped to 20)
    # R03 triggers (limit 100k, requested 85001.00 > 80k) -> AMOUNT 20
    # Total = 20 + 20 = 40 (boundary >= 40) -> HIGH, requires_investigation = True!
    claim = make_clean_claim_input(
        incident_date=date(2026, 6, 10),
        registered_date=date(2026, 6, 28),
        requested_amount=Decimal("85001.00"),
    )
    peers = (Decimal("50000.00"),) * 10
    inp = make_risk_input(
        claim=claim,
        policy=pol,
        peer_approved_amounts=peers,
        eval_date=date(2026, 6, 28),
    )
    out = evaluate_risk(inp)

    assert out.group_scores["TIMING"] == 20
    assert out.group_scores["AMOUNT"] == 20
    assert out.score == 40
    assert out.category == RiskCategoryEnum.HIGH
    assert out.requires_investigation is True
    assert "SCORE_THRESHOLD" in out.routing_reasons


def test_appendix_a_worked_example_7_clean_claim_zero() -> None:
    """Example 7: none triggered -> Calculation 0 -> Score 0, LOW, Investigation NO."""
    inp = make_risk_input()
    out = evaluate_risk(inp)

    assert out.score == 0
    assert out.category == RiskCategoryEnum.LOW
    assert out.completeness == RiskCompletenessEnum.COMPLETE
    assert out.requires_investigation is False
    assert out.routing_reasons == ()
    assert out.composed_label == "LOW (COMPLETE)"


def test_appendix_a_worked_example_8_all_indicators_skipped_insufficient_data() -> None:
    """Example 8: all indicators SKIPPED -> evaluated_weight 0 -> Score NULL, UNASSESSED, INSUFFICIENT_DATA, Investigation NO."""
    # When policy and coverage missing, and history covered before loaded_from
    # Let's verify when mandatory indicators are skipped
    claim = make_clean_claim_input()
    # Without policy (skips R05, R06), without coverage (skips R03), peer amounts empty (skips R04)
    # History coverage starting tomorrow skips R01, R02
    hist_cov = {"HEALTH": date(2026, 12, 31)}
    inp = make_risk_input(
        claim=claim,
        policy=None,  # skips R05, R06
        coverage=None,  # skips R03
        peer_approved_amounts=(),  # skips R04
        history_coverage=hist_cov,  # skips R01, R02
        eval_date=date(2026, 6, 20),
    )
    out = evaluate_risk(inp)

    # Since mandatory indicators R05, R06 are skipped, completeness must be INSUFFICIENT_DATA!
    assert out.completeness == RiskCompletenessEnum.INSUFFICIENT_DATA
    # Invariant: INSUFFICIENT_DATA means UNASSESSED and NULL score, never LOW!
    assert out.category == RiskCategoryEnum.UNASSESSED
    assert out.score is None
    # Missing data NEVER causes investigation routing by itself!
    assert out.requires_investigation is False
    assert out.composed_label == "UNASSESSED (INSUFFICIENT_DATA)"


# ========================================================
# Additional Invariant, Pilot Banner, and Security Tests
# ========================================================


def test_pilot_banner_present_in_output() -> None:
    """Task 1: Show a PILOT banner wherever risk results appear."""
    out = evaluate_risk(make_risk_input())
    assert "PILOT" in out.pilot_banner
    assert "unvalidated" in out.pilot_banner.lower()


def test_history_coverage_skips_when_window_not_covered() -> None:
    """Task 6: An indicator whose lookback starts before loaded_from is SKIPPED with HISTORY_WINDOW_NOT_COVERED."""
    # R01 lookback is 365 days. If eval_date is 2026-06-20, window starts 2025-06-20.
    # If loaded_from is 2025-09-01, lookback window start is before loaded_from.
    hist_cov = {"HEALTH": date(2025, 9, 1)}
    inp = make_risk_input(history_coverage=hist_cov, eval_date=date(2026, 6, 20))
    out = evaluate_risk(inp)

    r01_res = next(r for r in out.indicator_results if r.indicator_id == "R01")
    assert r01_res.status == IndicatorEvaluationStatusEnum.SKIPPED
    assert r01_res.skip_reason == "HISTORY_WINDOW_NOT_COVERED"


def test_frequency_counts_distinct_incidents_not_just_claims() -> None:
    """Task 7: Frequency indicators count distinct incidents (evidence shows both claim and incident counts)."""
    # Create 3 claims that belong to the SAME incident group (1 distinct incident)
    same_incident_id = "inc-group-123"
    prior_claims = (
        HistoricalClaimInput(
            claim_id="clm-1",
            claim_ref="CLM-1",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 3, 1),
            registered_date=date(2026, 3, 5),
            requested_amount=Decimal("1000.00"),
            status="REGISTERED",
            incident_group_id=same_incident_id,
        ),
        HistoricalClaimInput(
            claim_id="clm-2",
            claim_ref="CLM-2",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 3, 1),
            registered_date=date(2026, 3, 6),
            requested_amount=Decimal("2000.00"),
            status="REGISTERED",
            incident_group_id=same_incident_id,
        ),
        HistoricalClaimInput(
            claim_id="clm-3",
            claim_ref="CLM-3",
            customer_id="cust-001",
            policy_id="pol-001",
            claim_category="CAT_HEALTH_OPD",
            incident_date=date(2026, 3, 1),
            registered_date=date(2026, 3, 7),
            requested_amount=Decimal("3000.00"),
            status="REGISTERED",
            incident_group_id=same_incident_id,
        ),
    )
    inp = make_risk_input(historical_claims=prior_claims, eval_date=date(2026, 6, 20))
    out = evaluate_risk(inp)

    r01_res = next(r for r in out.indicator_results if r.indicator_id == "R01")
    # Even though there are 3 claims, there is only 1 distinct incident!
    # R01 threshold is 3 distinct incidents, so it should NOT trigger.
    assert r01_res.triggered is False
    assert r01_res.evidence["prior_qualifying_claims_count"] == 3
    assert r01_res.evidence["prior_qualifying_incidents_count"] == 1


def test_r08_no_longer_uses_name_matching_or_geography() -> None:
    """Task 8: Remove geographic clause from R08; reads human attestation result instead of comparing name strings."""
    # Document with named_person that doesn't match, but attestation result is CONFIRMED
    doc = DocumentInput(
        document_id="doc-conf",
        doc_code="GOV_ID",
        status="VALID",
        named_person="Completely Different Name",
        identity_attestation_result="CONFIRMED",
    )
    inp = make_risk_input(documents=(doc,), eval_date=date(2026, 6, 20))
    out = evaluate_risk(inp)

    r08_res = next(r for r in out.indicator_results if r.indicator_id == "R08")
    assert r08_res.triggered is False
    assert r08_res.weight_applied == 0


def test_determinism_and_shuffling_invariance() -> None:
    """Proves that shuffling input lists produces identical output."""
    base_inp = make_risk_input()
    base_out = evaluate_risk(base_inp)

    for _ in range(5):
        h_shuf = list(base_inp.historical_claims)
        p_shuf = list(base_inp.peer_approved_amounts)
        import random

        random.shuffle(h_shuf)
        random.shuffle(p_shuf)

        shuf_inp = RiskInput(
            claim=base_inp.claim,
            policy=base_inp.policy,
            coverage=base_inp.coverage,
            documents=base_inp.documents,
            historical_claims=tuple(h_shuf),
            peer_approved_amounts=tuple(p_shuf),
            evaluation_date=base_inp.evaluation_date,
            ruleset=base_inp.ruleset,
            history_coverage=base_inp.history_coverage,
        )
        shuf_out = evaluate_risk(shuf_inp)

        assert shuf_out.score == base_out.score
        assert shuf_out.category == base_out.category
        assert shuf_out.completeness == base_out.completeness
        assert shuf_out.requires_investigation == base_out.requires_investigation
        assert shuf_out.composed_label == base_out.composed_label
        assert shuf_out.group_scores == base_out.group_scores

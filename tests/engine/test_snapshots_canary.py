"""Canary PII leak tests and extra-field rejection tests for engine snapshots (P5 Task 9)."""

import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.engine.assessment_cap import calculate_assessment_cap
from app.engine.risk import evaluate_risk
from app.engine.types import (
    AssessmentComponentInput,
    AssessmentInput,
    ClaimInput,
    CoverageInput,
    PolicyInput,
    RiskInput,
    ValidationInput,
)
from app.engine.validation import evaluate_validation
from app.rules.loader import ACTIVE_RULESET
from app.schemas.snapshots import (
    AssessmentSnapshotModel,
    RiskSnapshotModel,
    ValidationSnapshotModel,
    deserialize_assessment_snapshot,
    deserialize_risk_snapshot,
    deserialize_validation_snapshot,
    serialize_assessment_snapshot,
    serialize_risk_snapshot,
    serialize_validation_snapshot,
)

CANARY_NAME = "CANARY_PERSON_NAME_LEAK"
CANARY_POLICY_NO = "POL-CANARY-777777"
CANARY_LOCATION = "Canary City Central Station Platform 9"
CANARY_DESCRIPTION = "Canary clinical confidential medical notes and injury detail"


def test_models_reject_extra_fields() -> None:
    """Task 9: All snapshot models must forbid extra fields at runtime."""
    base_claim = {
        "claim_id": "c1",
        "customer_id": "cust1",
        "policy_id": "pol1",
        "claim_category": "ACCIDENT",
        "incident_date": "2026-05-01",
        "registered_date": "2026-05-02",
        "requested_amount": "50000.00",
    }

    # ValidationSnapshotModel extra field
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ValidationSnapshotModel.model_validate(
            {
                "claim": base_claim,
                "coverages": [],
                "exclusions": [],
                "documents": [],
                "required_document_types": [],
                "evaluation_date": "2026-05-02",
                "ruleset_version": "v1",
                "engine_version": "1.0.0",
                "unauthorized_extra_field": "leak",
            }
        )

    # RiskSnapshotModel extra field
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RiskSnapshotModel.model_validate(
            {
                "claim": base_claim,
                "documents": [],
                "historical_claims": [],
                "peer_approved_amounts": [],
                "evaluation_date": "2026-05-02",
                "ruleset_version": "v1",
                "engine_version": "1.0.0",
                "customer_name": CANARY_NAME,  # Forbidden PII
            }
        )

    # AssessmentSnapshotModel extra field
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AssessmentSnapshotModel.model_validate(
            {
                "product_type": "HEALTH",
                "requested_amount": "10000.00",
                "remaining_limit": "50000.00",
                "assessed_loss": "10000.00",
                "deductible": "0.00",
                "copay_percent": "0.00",
                "depreciation": "0.00",
                "sum_insured": "0.00",
                "value_at_risk": "0.00",
                "leaked_claimant_id": "12345",
            }
        )


def test_validation_snapshot_canary_leak_protection() -> None:
    """Canary test: Validation snapshot must never contain free text or policy numbers."""
    claim = ClaimInput(
        claim_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        claim_ref=CANARY_POLICY_NO,
        customer_id="4fa85f64-5717-4562-b3fc-2c963f66afa7",
        policy_id="5fa85f64-5717-4562-b3fc-2c963f66afa8",
        claim_category="INPATIENT",
        incident_date=date(2026, 6, 1),
        incident_location=CANARY_LOCATION,
        incident_description=CANARY_DESCRIPTION,
        requested_amount=Decimal("15000.00"),
        registered_date=date(2026, 6, 2),
    )
    policy = PolicyInput(
        policy_id="5fa85f64-5717-4562-b3fc-2c963f66afa8",
        policy_no=CANARY_POLICY_NO,
        customer_id="4fa85f64-5717-4562-b3fc-2c963f66afa7",
        product_type="HEALTH",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        status="ACTIVE",
    )
    val_input = ValidationInput(
        claim=claim,
        policy=policy,
        coverages=(
            CoverageInput(
                coverage_id="cov1",
                claim_category="INPATIENT",
                limit_amount=Decimal("100000.00"),
                consumed_amount=Decimal("0.00"),
                deductible=Decimal("1000.00"),
                copay_percent=Decimal("10.00"),
                waiting_period_days=0,
                is_excluded=False,
            ),
        ),
        exclusions=(),
        documents=(),
        required_document_types=(),
        evaluation_date=date(2026, 6, 2),
        ruleset=ACTIVE_RULESET,
    )

    snapshot = serialize_validation_snapshot(val_input)
    snapshot_json = json.dumps(snapshot)

    # Assert no canaries leaked
    assert CANARY_POLICY_NO not in snapshot_json
    assert CANARY_LOCATION not in snapshot_json
    assert CANARY_DESCRIPTION not in snapshot_json
    assert "policy_no" not in snapshot_json

    # Assert roundtrip determinism
    reconstructed_input = deserialize_validation_snapshot(snapshot, ruleset=ACTIVE_RULESET)
    orig_output = evaluate_validation(val_input)
    replayed_output = evaluate_validation(reconstructed_input)

    assert orig_output.overall_result == replayed_output.overall_result
    assert len(orig_output.rule_results) == len(replayed_output.rule_results)


def test_risk_snapshot_canary_leak_protection() -> None:
    """Canary test: Risk snapshot must never contain customer name, free text, or policy numbers."""
    claim = ClaimInput(
        claim_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        claim_ref="CLM-2026-000001",
        customer_id="4fa85f64-5717-4562-b3fc-2c963f66afa7",
        policy_id="5fa85f64-5717-4562-b3fc-2c963f66afa8",
        claim_category="INPATIENT",
        incident_date=date(2026, 6, 1),
        incident_location=CANARY_LOCATION,
        incident_description=CANARY_DESCRIPTION,
        requested_amount=Decimal("15000.00"),
        registered_date=date(2026, 6, 2),
    )
    policy = PolicyInput(
        policy_id="5fa85f64-5717-4562-b3fc-2c963f66afa8",
        policy_no=CANARY_POLICY_NO,
        customer_id="4fa85f64-5717-4562-b3fc-2c963f66afa7",
        product_type="HEALTH",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        status="ACTIVE",
    )
    risk_input = RiskInput(
        claim=claim,
        policy=policy,
        coverage=None,
        documents=(),
        historical_claims=(),
        peer_approved_amounts=(),
        evaluation_date=date(2026, 6, 2),
        ruleset=ACTIVE_RULESET,
    )

    snapshot = serialize_risk_snapshot(risk_input)
    snapshot_json = json.dumps(snapshot)

    # Assert no canaries leaked
    assert CANARY_NAME not in snapshot_json
    assert CANARY_POLICY_NO not in snapshot_json
    assert CANARY_LOCATION not in snapshot_json
    assert CANARY_DESCRIPTION not in snapshot_json
    assert "customer_name" not in snapshot_json
    assert "policy_no" not in snapshot_json

    # Assert roundtrip determinism
    reconstructed_input = deserialize_risk_snapshot(snapshot, ruleset=ACTIVE_RULESET)
    orig_output = evaluate_risk(risk_input)
    replayed_output = evaluate_risk(reconstructed_input)

    assert orig_output.score == replayed_output.score
    assert orig_output.category == replayed_output.category
    assert orig_output.requires_investigation == replayed_output.requires_investigation


def test_assessment_snapshot_canary_and_roundtrip() -> None:
    """Canary test: Assessment snapshot roundtrip and determinism."""
    inp = AssessmentInput(
        product_type="HEALTH",
        requested_amount=Decimal("100000.00"),
        remaining_limit=Decimal("200000.00"),
        assessed_loss=Decimal("100000.00"),
        deductible=Decimal("5000.00"),
        copay_percent=Decimal("10.00"),
        components=(
            AssessmentComponentInput(
                component_type="ROOM_RENT",
                claimed_amount=Decimal("40000.00"),
                assessed_amount=Decimal("40000.00"),
            ),
            AssessmentComponentInput(
                component_type="SURGERY",
                claimed_amount=Decimal("60000.00"),
                assessed_amount=Decimal("60000.00"),
            ),
        ),
    )
    snapshot = serialize_assessment_snapshot(inp)
    assert json.loads(json.dumps(snapshot)) == snapshot

    assert "product_type" in snapshot
    assert snapshot["assessed_loss"] == "100000.00"

    reconstructed = deserialize_assessment_snapshot(snapshot)
    orig_out = calculate_assessment_cap(inp)
    replayed_out = calculate_assessment_cap(reconstructed)

    assert orig_out.max_eligible_amount == replayed_out.max_eligible_amount
    assert orig_out.calculation_trace == replayed_out.calculation_trace

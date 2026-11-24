"""Production activation gate tests for Decision D11."""

import pytest

from app.main import validate_calculation_spec_activation


def test_production_rejects_illustrative_formulas_without_risk_acceptance() -> None:
    with pytest.raises(RuntimeError, match="recorded owner risk-acceptance reference"):
        validate_calculation_spec_activation(
            environment="production",
            calculation_spec_status="ILLUSTRATIVE",
            risk_acceptance_ref="",
        )


def test_production_allows_illustrative_formulas_with_recorded_acceptance() -> None:
    validate_calculation_spec_activation(
        environment="production",
        calculation_spec_status="ILLUSTRATIVE",
        risk_acceptance_ref="RISK-ACCEPTANCE-RECORD-2026-001",
    )


def test_production_allows_approved_calculation_spec_without_interim_acceptance() -> None:
    validate_calculation_spec_activation(
        environment="production",
        calculation_spec_status="APPROVED",
        risk_acceptance_ref="",
    )


def test_staging_can_use_illustrative_calculation_spec() -> None:
    validate_calculation_spec_activation(
        environment="staging",
        calculation_spec_status="ILLUSTRATIVE",
        risk_acceptance_ref="",
    )

"""Mechanical test verifying zero protected attributes or demographic proxies in RiskInput schema."""

from dataclasses import fields, is_dataclass

from app.engine.types import (
    ClaimInput,
    CoverageInput,
    DocumentInput,
    HistoricalClaimInput,
    PolicyInput,
    RiskInput,
)

FORBIDDEN_PROTECTED_TOKENS = {
    "age",
    "dob",
    "birth",
    "gender",
    "sex",
    "religion",
    "faith",
    "ethnicity",
    "race",
    "caste",
    "tribe",
    "disability",
    "handicap",
    "marital",
    "marriage",
    "spouse",
    "customer_name",
    "name",
    "customer_state",
    "region",
    "state_region",
    "postal",
    "zip",
    "geo",
}


def test_risk_input_schema_has_zero_protected_attributes() -> None:
    """Inspect RiskInput and all embedded dataclasses to prove zero protected attributes exist."""
    models_to_check = [
        RiskInput,
        ClaimInput,
        PolicyInput,
        CoverageInput,
        DocumentInput,
        HistoricalClaimInput,
    ]

    for model in models_to_check:
        assert is_dataclass(model), f"{model} must be a dataclass"
        model_field_names = [f.name.lower() for f in fields(model)]

        # Explicitly verify no field contains forbidden protected attribute tokens
        for field_name in model_field_names:
            # Allow 'named_person' on DocumentInput only for document metadata, but check other fields
            if model is DocumentInput and field_name == "named_person":
                continue
            field_parts = set(field_name.split("_"))
            for token in FORBIDDEN_PROTECTED_TOKENS:
                assert token not in field_parts, (
                    f"Protected attribute / proxy token '{token}' found in "
                    f"{model.__name__}.{field_name}"
                )

    # Explicit check on RiskInput itself
    risk_input_fields = {f.name for f in fields(RiskInput)}
    assert "customer_name" not in risk_input_fields
    assert "customer_state_region" not in risk_input_fields

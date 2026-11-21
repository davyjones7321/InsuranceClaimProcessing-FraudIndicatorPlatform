"""Pydantic validation schemas for insurance business rulesets.

Enforces strict schema validation, forbidding unknown keys, float values,
and any evidence level named VALIDATED.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RiskGroupSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cap: int = Field(gt=0, description="Maximum aggregated score cap for the group")
    description: str | None = Field(default=None, description="Human-readable description of group")


class RiskThresholdsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_score_threshold: int = Field(ge=0, le=100)
    low_max: int = Field(ge=0, le=100)
    medium_max: int = Field(ge=0, le=100)
    high_min: int = Field(ge=0, le=100)
    partial_min_pct: int = Field(ge=0, le=100, default=60)
    mandatory_indicators: list[str] = Field(default_factory=list)
    pilot_banner: str = Field(
        default="PILOT: Risk indicators are experimental and unvalidated. Manual review required."
    )


class RiskIndicatorConfigSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    rationale: str
    enabled: bool
    group: str
    weight: int = Field(ge=0)
    sufficient_alone: bool = Field(default=False)
    evidence_level: str
    applicable_products: list[str] = Field(
        default_factory=lambda: ["HEALTH", "VEHICLE", "PROPERTY"]
    )

    # Indicator specific parameters (strictly typed integers / strings, never floats)
    threshold_incidents: int | None = None
    threshold_claims: int | None = None
    window_days: int | None = None
    limit_ratio_threshold_percent: int | None = None
    peer_multiple_threshold: int | None = None
    min_historical_claims: int | None = None
    early_claim_window_days: int | None = None
    late_policy_window_days: int | None = None
    late_filing_window_days: int | None = None
    amount_variance_percent: int | None = None
    round_multiple: int | None = None
    min_amount: int | None = None

    @field_validator("evidence_level")
    @classmethod
    def validate_evidence_level(cls, v: str) -> str:
        if v.upper() == "VALIDATED":
            raise ValueError("Evidence level must not be named VALIDATED. Allowed: NONE, REVIEWED")
        if v.upper() not in ("NONE", "REVIEWED"):
            raise ValueError(f"Invalid evidence level '{v}'. Allowed: NONE, REVIEWED")
        return v.upper()


class RulesetSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruleset_version: str
    engine_version: str
    claim_limit_policy: str = Field(default="CAP_WITH_WARNING")
    validation_rules: dict[str, Any] = Field(default_factory=dict)
    risk_groups: dict[str, RiskGroupSchema]
    risk_thresholds: RiskThresholdsSchema
    risk_indicators: dict[str, RiskIndicatorConfigSchema]

    @model_validator(mode="after")
    def validate_cross_references(self) -> "RulesetSchema":
        # Ensure all indicator groups exist in risk_groups
        valid_groups = set(self.risk_groups.keys())
        for ind_id, ind_cfg in self.risk_indicators.items():
            if ind_cfg.group not in valid_groups:
                raise ValueError(
                    f"Indicator '{ind_id}' references unknown group '{ind_cfg.group}'. "
                    f"Valid groups: {sorted(valid_groups)}"
                )

        # Ensure all mandatory indicators exist in risk_indicators
        valid_indicators = set(self.risk_indicators.keys())
        for m_id in self.risk_thresholds.mandatory_indicators:
            if m_id not in valid_indicators:
                raise ValueError(f"Mandatory indicator '{m_id}' is not defined in risk_indicators")

        return self


def validate_ruleset_dict(data: dict[str, Any]) -> RulesetSchema:
    """Validate a parsed ruleset dictionary against the strict schema."""
    return RulesetSchema.model_validate(data)

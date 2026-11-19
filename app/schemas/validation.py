"""Pydantic schemas for Validation Run responses and query models."""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.domain.enums import RuleOutcome, ValidationOutcome


class ValidationRuleResultResponse(BaseModel):
    """Result of an individual evaluated validation rule."""

    model_config = ConfigDict(from_attributes=True)

    rule_id: str
    rule_version: str
    outcome: RuleOutcome
    reason_code: str | None = None
    message: str
    inputs_snapshot: dict[str, Any]


class ValidationRunResponse(BaseModel):
    """Full execution summary of a validation run."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_id: uuid.UUID
    ruleset_version: str
    engine_version: str
    evaluated_on: date
    overall_result: ValidationOutcome
    created_at: datetime
    results: list[ValidationRuleResultResponse]

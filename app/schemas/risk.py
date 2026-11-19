"""Pydantic schemas for Risk Evaluation responses."""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.domain.enums import IndicatorStatus, RiskCategory


class RiskIndicatorResultResponse(BaseModel):
    """Result of an individual evaluated risk indicator."""

    model_config = ConfigDict(from_attributes=True)

    indicator_id: str
    indicator_version: str
    triggered: bool
    weight_applied: int
    evidence: dict[str, Any]
    message: str
    status: IndicatorStatus


class RiskEvaluationResponse(BaseModel):
    """Execution summary of a claim risk evaluation run."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_id: uuid.UUID
    ruleset_version: str
    engine_version: str
    evaluated_on: date
    score: int
    category: RiskCategory
    requires_investigation: bool
    inputs_snapshot: dict[str, Any]
    created_at: datetime
    indicator_results: list[RiskIndicatorResultResponse]

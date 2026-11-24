"""Pydantic schemas for Risk Evaluation, Governance, and Incident Groups."""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import RiskCategory, RiskCompleteness


class RiskIndicatorResultResponse(BaseModel):
    """Result of an individual evaluated risk indicator."""

    model_config = ConfigDict(from_attributes=True)

    indicator_id: str
    indicator_version: str
    group: str = ""
    weight: int = 0
    triggered: bool
    weight_applied: int
    sufficient_alone: bool = False
    evidence_level: str = "NONE"
    skip_reason: str | None = None
    evidence: dict[str, Any]
    message: str
    status: str


class RiskEvaluationResponse(BaseModel):
    """Execution summary of a claim risk evaluation run."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_id: uuid.UUID
    ruleset_version: str
    engine_version: str
    evaluated_on: date
    score: int | None = None
    category: RiskCategory
    completeness: RiskCompleteness
    composed_label: str
    requires_investigation: bool
    routing_reasons: list[str] = Field(default_factory=list)
    pilot_banner: str
    group_scores: dict[str, int] = Field(default_factory=dict)
    inputs_snapshot: dict[str, Any]
    created_at: datetime
    indicator_results: list[RiskIndicatorResultResponse]


class CompletenessWaiverRequest(BaseModel):
    """Request payload for recording a manager risk completeness waiver."""

    remarks: str = Field(min_length=20, description="Mandatory manager justification remarks")


class ManualReferralRequest(BaseModel):
    """Request payload for manually referring a claim to investigation."""

    remarks: str = Field(
        min_length=20, description="Mandatory justification remarks for manual referral"
    )


class IndicatorFeedbackItem(BaseModel):
    """Per-indicator feedback item."""

    status: str = Field(..., pattern="^(HELPFUL|UNHELPFUL|MISLEADING|NOT_EVALUATED)$")
    notes: str | None = Field(default=None, max_length=280)


class InvestigationFeedbackRequest(BaseModel):
    """Structured feedback on triggered indicators at investigation closure."""

    feedback: dict[str, IndicatorFeedbackItem | str | bool] = Field(
        description="Map of indicator_id to feedback object, status string, or boolean"
    )


class IncidentGroupMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_id: uuid.UUID
    customer_id: uuid.UUID
    rev: int
    is_active: bool
    created_at: datetime


class IncidentGroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    group_ref: str
    customer_id: uuid.UUID
    status: str
    proposed_by_id: uuid.UUID
    confirmed_by_id: uuid.UUID | None = None
    proposal_reason: str
    created_at: datetime
    members: list[IncidentGroupMemberResponse] = Field(default_factory=list)


class IncidentGroupProposeRequest(BaseModel):
    claim_ids: list[uuid.UUID] = Field(min_length=2, max_length=10)
    reason: str = Field(min_length=10)


class IncidentGroupConfirmRequest(BaseModel):
    remarks: str = Field(min_length=10)


class IncidentGroupUnlinkRequest(BaseModel):
    claim_id: uuid.UUID
    reason: str = Field(min_length=5)
    remarks: str = Field(min_length=20)


class IncidentGroupMergeRequest(BaseModel):
    target_group_id: uuid.UUID
    reason: str = Field(min_length=5)
    remarks: str = Field(min_length=20)


class IncidentGroupSplitRequest(BaseModel):
    claim_ids_to_split: list[uuid.UUID] = Field(min_length=1)
    reason: str = Field(min_length=5)
    remarks: str = Field(min_length=20)

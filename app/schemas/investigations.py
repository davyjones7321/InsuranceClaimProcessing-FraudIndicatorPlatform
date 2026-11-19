"""Pydantic schemas for Investigation workflows and findings."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import InvestigationOutcome, InvestigationStatus


class InvestigationAssignRequest(BaseModel):
    """Payload for assigning an investigator to an investigation."""

    investigator_id: uuid.UUID


class InvestigationFindingCreate(BaseModel):
    """Payload for recording a new finding under an active investigation."""

    finding_text: Annotated[str, Field(min_length=5)]


class InvestigationFindingResponse(BaseModel):
    """API representation of an investigation finding."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    investigation_id: uuid.UUID
    author_id: uuid.UUID
    finding_text: str
    created_at: datetime


class InvestigationEscalateRequest(BaseModel):
    """Payload for escalating an investigation to managerial review."""

    reason: Annotated[str, Field(min_length=5)]


class InvestigationCompleteRequest(BaseModel):
    """Payload for concluding an investigation with a definitive outcome."""

    outcome: InvestigationOutcome
    remarks: Annotated[str | None, Field(default=None)] = None


class InvestigationResponse(BaseModel):
    """API representation of an investigation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_id: uuid.UUID
    previous_investigation_id: uuid.UUID | None = None
    assigned_to: uuid.UUID | None = None
    assigned_by: uuid.UUID
    status: InvestigationStatus
    outcome: InvestigationOutcome | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    created_at: datetime
    findings: list[InvestigationFindingResponse] = []

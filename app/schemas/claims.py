"""Pydantic schemas for Claim registration, status transitions, and queries."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import ClaimChannel, ClaimStatus


class ClaimCreate(BaseModel):
    """Payload for registering a new claim."""

    policy_id: uuid.UUID
    customer_id: uuid.UUID | None = Field(
        default=None,
        description="Required when registered by staff on behalf of customer. Derived for customer.",
    )
    insured_item_id: uuid.UUID | None = Field(
        default=None,
        description="Optional reference to insured vehicle, property, or member item.",
    )
    claim_category: Annotated[str, Field(min_length=2, max_length=64)]
    incident_date: date
    incident_location: Annotated[str, Field(min_length=2, max_length=255)]
    incident_description: Annotated[str, Field(min_length=5)]
    requested_amount: Annotated[Decimal, Field(gt=Decimal("0.00"), decimal_places=2)]
    source_system: Annotated[str | None, Field(default=None, max_length=64)] = None
    external_ref: Annotated[str | None, Field(default=None, max_length=128)] = None


class ClaimStatusTransitionRequest(BaseModel):
    """Payload for executing a state machine transition on a claim."""

    to_status: ClaimStatus
    expected_status_version: int = Field(
        ...,
        ge=1,
        description="Optimistic locking token matching the current claim status_version.",
    )
    remarks: Annotated[str | None, Field(default=None, min_length=5)] = None
    reason_code: Annotated[str | None, Field(default=None, max_length=64)] = None


class ClaimResponse(BaseModel):
    """Standard representation of a claim."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_ref: str
    policy_id: uuid.UUID
    customer_id: uuid.UUID
    insured_item_id: uuid.UUID | None = None
    claim_category: str
    incident_date: date
    incident_location: str
    incident_description: str
    requested_amount: Decimal
    registered_at: datetime
    registered_by: uuid.UUID
    channel: ClaimChannel
    status: ClaimStatus | str
    status_version: int
    source_system: str | None = None
    external_ref: str | None = None
    created_at: datetime
    updated_at: datetime


class ClaimStatusHistoryResponse(BaseModel):
    """Representation of an entry in claim status history."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_id: uuid.UUID
    from_status: str | None = None
    to_status: str
    changed_by: uuid.UUID
    reason: str | None = None
    created_at: datetime

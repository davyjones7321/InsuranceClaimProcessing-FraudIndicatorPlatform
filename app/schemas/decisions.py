"""Pydantic schemas for Claim Decisions."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import DecisionType


class DecisionCreate(BaseModel):
    """Payload for submitting a definitive claim decision."""

    decision: DecisionType
    approved_amount: Annotated[
        Decimal | None, Field(default=None, decimal_places=2, gt=Decimal("0.00"))
    ] = None
    remarks: Annotated[str, Field(min_length=20)]
    reason_codes: list[str] | None = None
    expected_status_version: int | None = Field(default=None, ge=1)


class DecisionResponse(BaseModel):
    """API representation of a claim decision."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_id: uuid.UUID
    decided_by: uuid.UUID
    decision: DecisionType
    approved_amount: Decimal | None = None
    limit_applied: Decimal | None = None
    remarks: str
    reason_codes: list[str] | None = None
    created_at: datetime

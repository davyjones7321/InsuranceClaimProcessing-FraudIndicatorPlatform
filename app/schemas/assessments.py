"""Pydantic schemas for Assessment lifecycle."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class AssessmentCreate(BaseModel):
    """Payload submitted by an assessor to record an assessment."""

    assessed_loss: Annotated[Decimal, Field(ge=Decimal("0.00"), decimal_places=2)]
    eligible_amount: Annotated[Decimal, Field(ge=Decimal("0.00"), decimal_places=2)]
    remarks: Annotated[str, Field(min_length=5)]
    supporting_info: Annotated[str | None, Field(default=None)] = None

    # Optional manual overrides/additions to coverage parameters
    deductible: Annotated[Decimal | None, Field(default=None, ge=Decimal("0.00"))] = None
    copay_percent: Annotated[Decimal | None, Field(default=None, ge=Decimal("0.00"), le=Decimal("100.00"))] = None
    depreciation: Annotated[Decimal | None, Field(default=None, ge=Decimal("0.00"))] = None
    value_at_risk: Annotated[Decimal | None, Field(default=None, ge=Decimal("0.00"))] = None


class AssessmentResponse(BaseModel):
    """API representation of a recorded assessment."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_id: uuid.UUID
    assessor_id: uuid.UUID
    assessed_loss: Decimal
    max_eligible_amount: Decimal
    eligible_amount: Decimal
    calculation_trace: dict[str, Any]
    remarks: str
    supporting_info: str | None = None
    created_at: datetime

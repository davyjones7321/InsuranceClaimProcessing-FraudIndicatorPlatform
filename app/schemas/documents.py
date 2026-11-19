"""Pydantic schemas for Claim Document uploads, review, and queries."""

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import DocumentStatus


class DocumentReviewRequest(BaseModel):
    """Payload for reviewing a claim document (Assessor / Staff)."""

    status: DocumentStatus
    reason_code: Annotated[str | None, Field(default=None, max_length=64)] = None
    review_remarks: Annotated[str | None, Field(default=None, min_length=5)] = None

    @model_validator(mode="after")
    def validate_invalid_status_requirements(self) -> "DocumentReviewRequest":
        """Section B.17: Marking a document INVALID requires a reason code and remarks."""
        if self.status == DocumentStatus.INVALID:
            if not self.reason_code or not self.reason_code.strip():
                raise ValueError("reason_code is mandatory when marking a document INVALID")
            if not self.review_remarks or not self.review_remarks.strip():
                raise ValueError("review_remarks is mandatory when marking a document INVALID")
        if self.status == DocumentStatus.SUPERSEDED:
            raise ValueError("SUPERSEDED status is system-managed and cannot be manually assigned")
        return self


class DocumentResponse(BaseModel):
    """Standard representation of a claim document."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_id: uuid.UUID
    doc_code: str
    file_name: str
    sha256: str
    size_bytes: int
    mime_type: str
    document_date: date | None = None
    named_person: str | None = None
    status: DocumentStatus
    reason_code: str | None = None
    review_remarks: str | None = None
    reviewed_by: uuid.UUID | None = None
    reviewed_at: datetime | None = None
    uploaded_by: uuid.UUID
    created_at: datetime

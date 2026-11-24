"""Pydantic schemas for Claim Document uploads, review, and queries."""

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import AttestationResult, DocumentStatus


class IdentityAttestationCreate(BaseModel):
    """Payload for submitting an identity attestation revision on an identity-bearing document."""

    result: AttestationResult
    reason_code: Annotated[str | None, Field(default=None, max_length=64)] = None
    remarks: Annotated[str | None, Field(default=None, min_length=5)] = None
    expected_rev: Annotated[
        int,
        Field(
            ge=0,
            description="Expected latest revision number. Use 0 if no prior attestation exists.",
        ),
    ]

    @model_validator(mode="after")
    def validate_result_requirements(self) -> "IdentityAttestationCreate":
        if self.result in (AttestationResult.MISMATCH, AttestationResult.UNCERTAIN):
            if not self.reason_code or not self.reason_code.strip():
                raise ValueError(
                    f"reason_code is mandatory when attestation result is {self.result.value}"
                )
            if not self.remarks or not self.remarks.strip():
                raise ValueError(
                    f"remarks is mandatory when attestation result is {self.result.value}"
                )
        return self


class IdentityAttestationResponse(BaseModel):
    """Representation of an identity attestation revision."""

    model_config = ConfigDict(from_attributes=True)

    document_id: uuid.UUID
    rev: int
    prev_rev: int | None = None
    document_sha256: str
    result: AttestationResult
    reason_code: str | None = None
    remarks: str | None = None
    reviewer_id: uuid.UUID
    reviewer_role: str
    subject_identity_version: int
    is_current: bool = False
    is_stale: bool = False
    created_at: datetime


class DocumentReviewRequest(BaseModel):
    """Payload for reviewing a claim document (Assessor / Manager)."""

    status: DocumentStatus
    reason_code: Annotated[str | None, Field(default=None, max_length=64)] = None
    review_remarks: Annotated[str | None, Field(default=None, min_length=5)] = None
    attestation: IdentityAttestationCreate | None = None

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
    is_identity_bearing: bool = False
    latest_attestation: IdentityAttestationResponse | None = None

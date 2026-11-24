"""Pydantic v2 validation schemas for master data entities and imports."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    LimitScope,
    PolicyStatus,
    ProductType,
)


# Customer Schemas
class CustomerCreate(BaseModel):
    customer_no: str = Field(..., min_length=3, max_length=64)
    full_name: str = Field(..., min_length=1, max_length=255)
    dob: date
    email: str = Field(..., max_length=255)
    phone: str = Field(..., min_length=7, max_length=32)
    address: str = Field(..., min_length=1)
    state_region: str = Field(..., min_length=2, max_length=128)
    source_system: str | None = Field(default=None, max_length=64)
    external_ref: str | None = Field(default=None, max_length=128)


class CustomerUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    dob: date | None = None
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, min_length=7, max_length=32)
    address: str | None = None
    state_region: str | None = Field(default=None, max_length=128)


class CustomerResponse(CustomerCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# Coverage Schemas
class CoverageCreate(BaseModel):
    claim_category: str = Field(..., min_length=1, max_length=64)
    limit_scope: LimitScope = Field(
        ..., description="Explicit limit scope: PER_POLICY_PERIOD or PER_CLAIM"
    )
    limit_amount: Decimal = Field(..., ge=0)
    deductible: Decimal = Field(..., ge=0, description="Deductible must be explicit (0.00 if none)")
    copay_percent: Decimal = Field(
        ..., ge=0, le=100, description="Copay percent must be explicit (0.00 if none)"
    )
    waiting_period_days: int = Field(default=0, ge=0)
    conditions: dict[str, Any] | None = None
    is_excluded: bool = False
    limit_exceeded_behavior: str = Field(
        ..., pattern="^(CAP|REJECT)$", description="Limit policy: CAP or REJECT"
    )


class CoverageUpdate(BaseModel):
    limit_amount: Decimal | None = Field(default=None, ge=0)
    deductible: Decimal | None = Field(default=None, ge=0)
    copay_percent: Decimal | None = Field(default=None, ge=0, le=100)
    waiting_period_days: int | None = Field(default=None, ge=0)
    conditions: dict[str, Any] | None = None
    is_excluded: bool | None = None
    limit_exceeded_behavior: str | None = Field(default=None, pattern="^(CAP|REJECT)$")


class CoverageResponse(CoverageCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    policy_id: uuid.UUID
    consumed_amount: Decimal
    created_at: datetime
    updated_at: datetime


# Exclusion Schemas
class ExclusionCreate(BaseModel):
    exclusion_code: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1)


class ExclusionResponse(ExclusionCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    policy_id: uuid.UUID
    created_at: datetime


# Policy Insured Item Schemas
class PolicyInsuredItemCreate(BaseModel):
    item_type: str = Field(..., min_length=1, max_length=64)  # MEMBER, VEHICLE, PROPERTY
    identifier: str = Field(..., min_length=1, max_length=128)
    details: dict[str, Any] | None = None


class PolicyInsuredItemResponse(PolicyInsuredItemCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    policy_id: uuid.UUID
    created_at: datetime


# Policy Schemas
class PolicyCreate(BaseModel):
    policy_no: str = Field(..., min_length=3, max_length=64)
    customer_id: uuid.UUID
    product_type: ProductType
    start_date: date
    end_date: date
    premium: Decimal = Field(..., ge=0)
    sum_insured: Decimal = Field(..., ge=0)
    status: PolicyStatus = PolicyStatus.ACTIVE
    source_system: str | None = Field(default=None, max_length=64)
    external_ref: str | None = Field(default=None, max_length=128)
    coverages: list[CoverageCreate] = Field(default_factory=list)
    exclusions: list[ExclusionCreate] = Field(default_factory=list)
    insured_items: list[PolicyInsuredItemCreate] = Field(default_factory=list)


class PolicyUpdate(BaseModel):
    status: PolicyStatus | None = None
    premium: Decimal | None = Field(default=None, ge=0)
    sum_insured: Decimal | None = Field(default=None, ge=0)
    end_date: date | None = None


class PolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    policy_no: str
    customer_id: uuid.UUID
    product_type: ProductType
    start_date: date
    end_date: date
    premium: Decimal
    sum_insured: Decimal
    status: PolicyStatus
    source_system: str | None
    external_ref: str | None
    created_at: datetime
    updated_at: datetime
    coverages: list[CoverageResponse] = Field(default_factory=list)
    exclusions: list[ExclusionResponse] = Field(default_factory=list)
    insured_items: list[PolicyInsuredItemResponse] = Field(default_factory=list)


# Required Document Type Schemas
class RequiredDocumentTypeCreate(BaseModel):
    product_type: ProductType
    claim_category: str | None = Field(default=None, max_length=64)
    doc_code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    is_mandatory: bool = True


class RequiredDocumentTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    is_mandatory: bool | None = None


class RequiredDocumentTypeResponse(RequiredDocumentTypeCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime


# Claim Import Schema (P7 Task 3)
class ClaimImportCreate(BaseModel):
    claim_ref: str = Field(..., min_length=3, max_length=64)
    policy_no: str = Field(..., min_length=3, max_length=64)
    incident_date: date
    claim_category: str = Field(..., min_length=1, max_length=64)
    requested_amount: Decimal = Field(..., gt=0)
    description: str = Field(..., min_length=1)
    channel: ClaimChannel = Field(default=ClaimChannel.CUSTOMER)
    source_system: str | None = Field(default=None, max_length=64)
    external_ref: str | None = Field(default=None, max_length=128)
    historical_status: ClaimStatus | None = Field(default=None)


# Master Data Import Schemas
class MasterDataBatchImport(BaseModel):
    customers: list[CustomerCreate] = Field(default_factory=list)
    policies: list[PolicyCreate] = Field(default_factory=list)
    required_documents: list[RequiredDocumentTypeCreate] = Field(default_factory=list)
    claims: list[ClaimImportCreate] = Field(default_factory=list)


class ImportSummary(BaseModel):
    dry_run: bool
    source_format: str
    customers_processed: int = 0
    policies_processed: int = 0
    coverages_processed: int = 0
    claims_processed: int = 0
    items_created: int = 0
    items_updated: int = 0
    rejects_created: int = 0
    batch_id: uuid.UUID | None = None
    errors: list[str] = Field(default_factory=list)

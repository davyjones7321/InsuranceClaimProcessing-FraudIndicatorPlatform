"""Pydantic schemas for Manager Analytics Dashboard and CSV Export."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.domain.enums import ClaimStatus, ProductType


class AnalyticsFilterParams(BaseModel):
    """Filtering criteria for analytics queries."""

    start_date: date | None = None
    end_date: date | None = None
    product_type: ProductType | None = None
    claim_category: str | None = None
    status: ClaimStatus | None = None


class VolumeByProduct(BaseModel):
    product_type: str
    count: int


class VolumeByStatus(BaseModel):
    status: str
    count: int


class VolumeOverTime(BaseModel):
    period: str
    count: int


class ProcessingTimeMetrics(BaseModel):
    overall_avg_hours: float
    overall_median_hours: float
    by_product: dict[str, float]


class ReasonCodeCount(BaseModel):
    reason_code: str
    count: int


class RiskMetrics(BaseModel):
    total_evaluations: int
    flagged_evaluations: int
    investigation_rate_percent: float
    category_distribution: dict[str, int]
    indicator_frequencies: dict[str, int]


class InvestigationMetrics(BaseModel):
    open_count: int
    escalated_count: int
    completed_count: int
    avg_duration_hours: float
    outcome_distribution: dict[str, int]


class ProductFinancialSummary(BaseModel):
    requested: Decimal
    approved: Decimal


class FinancialMetrics(BaseModel):
    total_requested: Decimal
    total_approved: Decimal
    by_product: dict[str, ProductFinancialSummary]


class BacklogMetrics(BaseModel):
    total_active_backlog: int
    by_status: dict[str, int]
    by_age: dict[str, int]


class AnalyticsDashboardResponse(BaseModel):
    """Aggregated manager dashboard containing all operational and risk metrics."""

    model_config = ConfigDict(from_attributes=True)

    volume_by_product: list[VolumeByProduct]
    volume_by_status: list[VolumeByStatus]
    volume_over_time: list[VolumeOverTime]
    processing_time: ProcessingTimeMetrics
    rejection_reasons: list[ReasonCodeCount]
    validation_failure_reasons: list[ReasonCodeCount]
    risk_metrics: RiskMetrics
    investigation_metrics: InvestigationMetrics
    financial_metrics: FinancialMetrics
    backlog_metrics: BacklogMetrics

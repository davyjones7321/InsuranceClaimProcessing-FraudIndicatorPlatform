"""Analytics dashboard and CSV export API router."""

from datetime import date

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.analytics.service import AnalyticsService
from app.db.models import User
from app.db.session import get_db
from app.domain.enums import ClaimStatus, ProductType, UserRole
from app.schemas.analytics import AnalyticsDashboardResponse
from app.security.rbac import get_current_user, require_roles

router = APIRouter(prefix="/api/analytics", tags=["Analytics & Reporting"])


@router.get(
    "/dashboard",
    response_model=AnalyticsDashboardResponse,
    status_code=status.HTTP_200_OK,
    summary="Get managerial analytics dashboard metrics",
    dependencies=[require_roles(UserRole.MANAGER, UserRole.ADMIN)],
)
def get_analytics_dashboard(
    start_date: date | None = Query(default=None, description="Earliest registration date"),
    end_date: date | None = Query(default=None, description="Latest registration date"),
    product_type: ProductType | None = Query(default=None, description="Filter by product line"),
    claim_category: str | None = Query(default=None, description="Filter by claim category"),
    claim_status: ClaimStatus | None = Query(
        default=None, alias="status", description="Filter by claim status"
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnalyticsDashboardResponse:
    """Compute and return aggregated operations, financial, and risk indicators."""
    service = AnalyticsService(db)
    return service.get_dashboard_metrics(
        start_date=start_date,
        end_date=end_date,
        product_type=product_type,
        claim_category=claim_category,
        status=claim_status,
    )


@router.get(
    "/export",
    status_code=status.HTTP_200_OK,
    summary="Export filtered claims report as CSV",
    dependencies=[require_roles(UserRole.MANAGER, UserRole.ADMIN)],
)
def export_analytics_csv(
    start_date: date | None = Query(default=None, description="Earliest registration date"),
    end_date: date | None = Query(default=None, description="Latest registration date"),
    product_type: ProductType | None = Query(default=None, description="Filter by product line"),
    claim_category: str | None = Query(default=None, description="Filter by claim category"),
    claim_status: ClaimStatus | None = Query(
        default=None, alias="status", description="Filter by claim status"
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Generate and download a CSV report of claims matching filter criteria."""
    service = AnalyticsService(db)
    csv_content = service.export_claims_csv(
        start_date=start_date,
        end_date=end_date,
        product_type=product_type,
        claim_category=claim_category,
        status=claim_status,
    )
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="claims_analytics_export.csv"'},
    )

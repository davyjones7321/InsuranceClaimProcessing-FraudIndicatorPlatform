"""Master data batch import router (Admin only)."""

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy.orm import Session

from app.db.models import User
from app.db.session import get_db
from app.domain.enums import UserRole
from app.schemas.master_data import ImportSummary, MasterDataBatchImport
from app.security.rbac import get_current_user, require_roles
from app.services.import_service import MasterDataImportService

router = APIRouter(prefix="/api/master-data/import", tags=["Master Data Import"])


@router.post(
    "/json",
    response_model=ImportSummary,
    status_code=status.HTTP_200_OK,
    summary="Batch import master data via JSON",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def import_master_data_json(
    payload: MasterDataBatchImport,
    dry_run: bool = Query(default=False, description="Validate without persisting"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ImportSummary:
    """Import customers, policies, and required document configurations in JSON format (Admin only)."""
    service = MasterDataImportService(
        db=db,
        actor_id=current_user.id,
        actor_role=current_user.role.value,
    )
    return service.import_json(payload=payload, dry_run=dry_run)


@router.post(
    "/csv",
    response_model=ImportSummary,
    status_code=status.HTTP_200_OK,
    summary="Batch import master data via CSV",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def import_master_data_csv(
    entity_type: str = Query(
        ...,
        description="Target entity type: 'customers', 'policies', 'coverages', or 'required_documents'",
    ),
    dry_run: bool = Query(default=False, description="Validate without persisting"),
    csv_text: str = Body(
        ..., media_type="text/plain", description="CSV string content with header row"
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ImportSummary:
    """Import entity rows from CSV text (Admin only)."""
    service = MasterDataImportService(
        db=db,
        actor_id=current_user.id,
        actor_role=current_user.role.value,
    )
    return service.import_csv(
        entity_type=entity_type,
        csv_content=csv_text,
        dry_run=dry_run,
    )

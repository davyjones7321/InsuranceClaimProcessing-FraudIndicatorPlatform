"""Investigation management and feedback API router."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.models import User
from app.db.session import get_db
from app.domain.enums import UserRole
from app.schemas.investigations import InvestigationResponse
from app.schemas.risk import InvestigationFeedbackRequest
from app.security.rbac import PermissionDeniedError, get_current_user, require_roles
from app.services.risk_service import RiskService, RiskServiceError

router = APIRouter(prefix="/api/investigations", tags=["Investigations"])


@router.post(
    "/{investigation_id}/feedback",
    response_model=InvestigationResponse,
    status_code=status.HTTP_200_OK,
    summary="Record structured investigator feedback on indicators",
    dependencies=[require_roles(UserRole.INVESTIGATOR, UserRole.MANAGER, UserRole.ADMIN)],
)
def record_investigation_feedback(
    investigation_id: uuid.UUID,
    payload: InvestigationFeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvestigationResponse:
    """Record per-indicator structured feedback at investigation closure (Task 13)."""
    service = RiskService(db)
    try:
        inv = service.record_investigation_feedback(
            investigation_id=investigation_id,
            actor=current_user,
            indicator_feedback=payload.feedback,
        )
        return InvestigationResponse.model_validate(inv)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RiskServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

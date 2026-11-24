"""API router for incident groups management and multi-claim linking (P6 Task 9)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.models import IncidentGroup, User
from app.db.session import get_db
from app.domain.enums import UserRole
from app.schemas.risk import (
    IncidentGroupConfirmRequest,
    IncidentGroupMemberResponse,
    IncidentGroupMergeRequest,
    IncidentGroupProposeRequest,
    IncidentGroupResponse,
    IncidentGroupSplitRequest,
    IncidentGroupUnlinkRequest,
)
from app.security.rbac import PermissionDeniedError, get_current_user, require_roles
from app.services.incident_group_service import IncidentGroupError, IncidentGroupService

router = APIRouter(prefix="/api/incident-groups", tags=["Incident Groups"])


def _serialize_group(group: IncidentGroup) -> IncidentGroupResponse:
    return IncidentGroupResponse(
        id=group.id,
        group_ref=group.group_ref,
        customer_id=group.customer_id,
        status=group.status.value,
        proposed_by_id=group.proposed_by_id,
        confirmed_by_id=group.confirmed_by_id,
        proposal_reason=group.proposal_reason,
        created_at=group.created_at,
        members=[
            IncidentGroupMemberResponse(
                id=m.id,
                claim_id=m.claim_id,
                customer_id=m.customer_id,
                rev=m.rev,
                is_active=m.is_active,
                created_at=m.created_at,
            )
            for m in group.members
        ],
    )


@router.post(
    "/propose",
    response_model=IncidentGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Propose incident group linking multiple claims of same customer (P6 Task 9)",
    dependencies=[
        require_roles(
            UserRole.STAFF,
            UserRole.ASSESSOR,
            UserRole.INVESTIGATOR,
            UserRole.MANAGER,
            UserRole.ADMIN,
        )
    ],
)
def propose_incident_group(
    payload: IncidentGroupProposeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IncidentGroupResponse:
    """Propose incident group with limits on date span and size."""
    service = IncidentGroupService(db)
    try:
        group = service.propose_group(
            actor=current_user,
            claim_ids=payload.claim_ids,
            reason=payload.reason,
        )
        return _serialize_group(group)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except IncidentGroupError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/{group_id}/confirm",
    response_model=IncidentGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm incident group with four-eyes separation (proposer != confirmer)",
    dependencies=[require_roles(UserRole.ASSESSOR, UserRole.MANAGER, UserRole.ADMIN)],
)
def confirm_incident_group(
    group_id: uuid.UUID,
    payload: IncidentGroupConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IncidentGroupResponse:
    """Confirm incident group. Confirmer cannot be the proposer."""
    service = IncidentGroupService(db)
    try:
        group = service.confirm_group(
            actor=current_user,
            group_id=group_id,
            remarks=payload.remarks,
        )
        return _serialize_group(group)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except IncidentGroupError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/{group_id}/unlink",
    response_model=IncidentGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Unlink claim from incident group (Manager only, P6 Task 9)",
    dependencies=[require_roles(UserRole.MANAGER, UserRole.ADMIN)],
)
def unlink_claim_from_group(
    group_id: uuid.UUID,
    payload: IncidentGroupUnlinkRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IncidentGroupResponse:
    """Manager unlinks a claim with append-only revisions."""
    service = IncidentGroupService(db)
    try:
        group = service.unlink_claim(
            manager=current_user,
            group_id=group_id,
            claim_id=payload.claim_id,
            reason=payload.reason,
            remarks=payload.remarks,
        )
        return _serialize_group(group)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except IncidentGroupError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/{group_id}/merge",
    response_model=IncidentGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Merge incident groups (Manager only)",
    dependencies=[require_roles(UserRole.MANAGER, UserRole.ADMIN)],
)
def merge_incident_groups(
    group_id: uuid.UUID,
    payload: IncidentGroupMergeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IncidentGroupResponse:
    """Manager merges source group into target group."""
    service = IncidentGroupService(db)
    try:
        group = service.merge_groups(
            manager=current_user,
            target_group_id=payload.target_group_id,
            source_group_id=group_id,
            reason=payload.reason,
            remarks=payload.remarks,
        )
        return _serialize_group(group)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except IncidentGroupError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/{group_id}/split",
    response_model=list[IncidentGroupResponse],
    status_code=status.HTTP_200_OK,
    summary="Split incident group (Manager only)",
    dependencies=[require_roles(UserRole.MANAGER, UserRole.ADMIN)],
)
def split_incident_group(
    group_id: uuid.UUID,
    payload: IncidentGroupSplitRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[IncidentGroupResponse]:
    """Manager splits claims into a new group."""
    service = IncidentGroupService(db)
    try:
        source, new_grp = service.split_group(
            manager=current_user,
            source_group_id=group_id,
            claim_ids_to_split=payload.claim_ids_to_split,
            reason=payload.reason,
            remarks=payload.remarks,
        )
        return [_serialize_group(source), _serialize_group(new_grp)]
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except IncidentGroupError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "/{group_id}",
    response_model=IncidentGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Get incident group details",
    dependencies=[
        require_roles(
            UserRole.STAFF,
            UserRole.ASSESSOR,
            UserRole.INVESTIGATOR,
            UserRole.MANAGER,
            UserRole.ADMIN,
        )
    ],
)
def get_incident_group(
    group_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IncidentGroupResponse:
    group = db.get(IncidentGroup, group_id)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Incident group '{group_id}' not found"
        )
    return _serialize_group(group)

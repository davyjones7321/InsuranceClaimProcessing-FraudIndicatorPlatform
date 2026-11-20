"""Coverages and exclusions router with RBAC and object-level authorization."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.db.models import Coverage, Exclusion, Policy, User
from app.db.session import get_db
from app.domain.enums import UserRole
from app.schemas.master_data import (
    CoverageCreate,
    CoverageResponse,
    CoverageUpdate,
    ExclusionCreate,
    ExclusionResponse,
)
from app.security.rbac import PermissionDeniedError, get_current_user, require_roles

router = APIRouter(tags=["Coverages & Exclusions"])


@router.post(
    "/api/policies/{policy_id}/coverages",
    response_model=CoverageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add coverage to policy",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def add_coverage(
    policy_id: uuid.UUID,
    payload: CoverageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CoverageResponse:
    """Add a new coverage to an existing policy (Admin only)."""
    policy = db.get(Policy, policy_id)
    if not policy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy with ID '{policy_id}' not found",
        )

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy_id,
        claim_category=payload.claim_category,
        limit_scope=payload.limit_scope,
        limit_amount=payload.limit_amount,
        consumed_amount=Decimal("0.00"),
        deductible=payload.deductible,
        copay_percent=payload.copay_percent,
        waiting_period_days=payload.waiting_period_days,
        conditions=payload.conditions,
        is_excluded=payload.is_excluded,
        limit_exceeded_behavior=payload.limit_exceeded_behavior,
    )
    db.add(coverage)
    db.flush()

    write_audit_log(
        db=db,
        action="ADD_COVERAGE",
        entity_type="Coverage",
        entity_id=str(coverage.id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={
            "policy_id": str(policy_id),
            "claim_category": coverage.claim_category,
            "limit_amount": str(coverage.limit_amount),
        },
    )

    return CoverageResponse.model_validate(coverage)


@router.get(
    "/api/policies/{policy_id}/coverages",
    response_model=list[CoverageResponse],
    summary="List coverages for policy",
)
def list_coverages(
    policy_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CoverageResponse]:
    """List coverages for a policy. Customers can view only their own policy coverages."""
    policy = db.get(Policy, policy_id)
    if not policy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy with ID '{policy_id}' not found",
        )

    if current_user.role == UserRole.CUSTOMER:
        if policy.customer_id != current_user.customer_id:
            raise PermissionDeniedError("Customers can view only their own policy coverages")

    stmt = (
        select(Coverage).where(Coverage.policy_id == policy_id).order_by(Coverage.created_at.asc())
    )
    coverages = list(db.scalars(stmt).all())
    return [CoverageResponse.model_validate(c) for c in coverages]


@router.put(
    "/api/coverages/{coverage_id}",
    response_model=CoverageResponse,
    summary="Update coverage",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def update_coverage(
    coverage_id: uuid.UUID,
    payload: CoverageUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CoverageResponse:
    """Update coverage limits and terms (Admin only)."""
    coverage = db.get(Coverage, coverage_id)
    if not coverage:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Coverage with ID '{coverage_id}' not found",
        )

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(coverage, field, value)

    db.flush()

    write_audit_log(
        db=db,
        action="UPDATE_COVERAGE",
        entity_type="Coverage",
        entity_id=str(coverage.id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={"updated_fields": list(update_data.keys())},
    )

    return CoverageResponse.model_validate(coverage)


@router.post(
    "/api/policies/{policy_id}/exclusions",
    response_model=ExclusionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add exclusion to policy",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def add_exclusion(
    policy_id: uuid.UUID,
    payload: ExclusionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ExclusionResponse:
    """Add an exclusion rule to a policy (Admin only)."""
    policy = db.get(Policy, policy_id)
    if not policy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy with ID '{policy_id}' not found",
        )

    exclusion = Exclusion(
        id=uuid.uuid4(),
        policy_id=policy_id,
        exclusion_code=payload.exclusion_code,
        description=payload.description,
    )
    db.add(exclusion)
    db.flush()

    write_audit_log(
        db=db,
        action="ADD_EXCLUSION",
        entity_type="Exclusion",
        entity_id=str(exclusion.id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={
            "policy_id": str(policy_id),
            "exclusion_code": exclusion.exclusion_code,
        },
    )

    return ExclusionResponse.model_validate(exclusion)


@router.get(
    "/api/policies/{policy_id}/exclusions",
    response_model=list[ExclusionResponse],
    summary="List exclusions for policy",
)
def list_exclusions(
    policy_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ExclusionResponse]:
    """List exclusions for a policy. Customers can view only their own policy exclusions."""
    policy = db.get(Policy, policy_id)
    if not policy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy with ID '{policy_id}' not found",
        )

    if current_user.role == UserRole.CUSTOMER:
        if policy.customer_id != current_user.customer_id:
            raise PermissionDeniedError("Customers can view only their own policy exclusions")

    stmt = (
        select(Exclusion)
        .where(Exclusion.policy_id == policy_id)
        .order_by(Exclusion.created_at.asc())
    )
    exclusions = list(db.scalars(stmt).all())
    return [ExclusionResponse.model_validate(e) for e in exclusions]


@router.delete(
    "/api/exclusions/{exclusion_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete exclusion",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def delete_exclusion(
    exclusion_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Delete an exclusion (Admin only)."""
    exclusion = db.get(Exclusion, exclusion_id)
    if not exclusion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exclusion with ID '{exclusion_id}' not found",
        )

    db.delete(exclusion)
    db.flush()

    write_audit_log(
        db=db,
        action="DELETE_EXCLUSION",
        entity_type="Exclusion",
        entity_id=str(exclusion_id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={"exclusion_code": exclusion.exclusion_code},
    )

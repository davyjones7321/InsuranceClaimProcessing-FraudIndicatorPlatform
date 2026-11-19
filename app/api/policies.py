"""Policy master data router with RBAC and object-level authorization."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.audit.writer import write_audit_log
from app.db.models import Coverage, Customer, Exclusion, Policy, PolicyInsuredItem, User
from app.db.session import get_db
from app.domain.enums import PolicyStatus, ProductType, UserRole
from app.schemas.master_data import PolicyCreate, PolicyResponse, PolicyUpdate
from app.security.rbac import PermissionDeniedError, get_current_user, require_roles

router = APIRouter(prefix="/api/policies", tags=["Policies"])


@router.post(
    "",
    response_model=PolicyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create policy",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def create_policy(
    payload: PolicyCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    """Create a new policy with optional coverages, exclusions, and insured items (Admin only)."""
    # Verify customer exists
    customer = db.get(Customer, payload.customer_id)
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID '{payload.customer_id}' not found",
        )

    # Check unique policy_no
    existing = db.scalars(select(Policy).where(Policy.policy_no == payload.policy_no)).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Policy with policy_no '{payload.policy_no}' already exists",
        )

    policy_id = uuid.uuid4()
    policy = Policy(
        id=policy_id,
        policy_no=payload.policy_no,
        customer_id=payload.customer_id,
        product_type=payload.product_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        premium=payload.premium,
        sum_insured=payload.sum_insured,
        status=payload.status,
        source_system=payload.source_system,
        external_ref=payload.external_ref,
    )
    db.add(policy)

    # Add nested coverages
    for cov_data in payload.coverages:
        coverage = Coverage(
            id=uuid.uuid4(),
            policy_id=policy_id,
            claim_category=cov_data.claim_category,
            limit_scope=cov_data.limit_scope,
            limit_amount=cov_data.limit_amount,
            consumed_amount=Decimal("0.00"),
            deductible=cov_data.deductible,
            copay_percent=cov_data.copay_percent,
            waiting_period_days=cov_data.waiting_period_days,
            conditions=cov_data.conditions,
            is_excluded=cov_data.is_excluded,
        )
        db.add(coverage)

    # Add nested exclusions
    for excl_data in payload.exclusions:
        exclusion = Exclusion(
            id=uuid.uuid4(),
            policy_id=policy_id,
            exclusion_code=excl_data.exclusion_code,
            description=excl_data.description,
        )
        db.add(exclusion)

    # Add nested insured items
    for item_data in payload.insured_items:
        insured_item = PolicyInsuredItem(
            id=uuid.uuid4(),
            policy_id=policy_id,
            item_type=item_data.item_type,
            identifier=item_data.identifier,
            details=item_data.details,
        )
        db.add(insured_item)

    db.flush()

    write_audit_log(
        db=db,
        action="CREATE_POLICY",
        entity_type="Policy",
        entity_id=str(policy.id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={
            "policy_no": policy.policy_no,
            "product_type": policy.product_type.value,
            "customer_id": str(policy.customer_id),
            "coverages_count": len(payload.coverages),
        },
    )

    # Reload policy with relationships
    stmt = (
        select(Policy)
        .options(
            selectinload(Policy.coverages),
            selectinload(Policy.exclusions),
            selectinload(Policy.insured_items),
        )
        .where(Policy.id == policy.id)
    )
    loaded = db.scalars(stmt).one()
    return PolicyResponse.model_validate(loaded)


@router.get(
    "",
    response_model=list[PolicyResponse],
    summary="List or search policies",
)
def list_policies(
    customer_id: uuid.UUID | None = Query(default=None, description="Filter by customer ID"),
    product_type: ProductType | None = Query(default=None, description="Filter by product type"),
    policy_status: PolicyStatus | None = Query(
        default=None, alias="status", description="Filter by status"
    ),
    search: str | None = Query(default=None, description="Search by policy_no"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PolicyResponse]:
    """List policies. Customers are restricted strictly to their own policies."""
    stmt = (
        select(Policy)
        .options(
            selectinload(Policy.coverages),
            selectinload(Policy.exclusions),
            selectinload(Policy.insured_items),
        )
        .order_by(Policy.created_at.desc())
    )

    if current_user.role == UserRole.CUSTOMER:
        # Enforce own customer ID regardless of what query param was passed
        if customer_id is not None and customer_id != current_user.customer_id:
            raise PermissionDeniedError("Customers can view only their own policies")
        stmt = stmt.where(Policy.customer_id == current_user.customer_id)
    else:
        if customer_id:
            stmt = stmt.where(Policy.customer_id == customer_id)

    if product_type:
        stmt = stmt.where(Policy.product_type == product_type)
    if policy_status:
        stmt = stmt.where(Policy.status == policy_status)
    if search:
        stmt = stmt.where(Policy.policy_no.ilike(f"%{search.strip()}%"))

    stmt = stmt.limit(limit).offset(offset)
    policies = list(db.scalars(stmt).all())
    return [PolicyResponse.model_validate(p) for p in policies]


@router.get(
    "/{policy_id}",
    response_model=PolicyResponse,
    summary="Get policy details",
)
def get_policy(
    policy_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    """Get policy details with coverages, exclusions, and insured items."""
    stmt = (
        select(Policy)
        .options(
            selectinload(Policy.coverages),
            selectinload(Policy.exclusions),
            selectinload(Policy.insured_items),
        )
        .where(Policy.id == policy_id)
    )
    policy = db.scalars(stmt).first()
    if not policy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy with ID '{policy_id}' not found",
        )

    if current_user.role == UserRole.CUSTOMER:
        if policy.customer_id != current_user.customer_id:
            raise PermissionDeniedError("Customers can view only their own policies")

    return PolicyResponse.model_validate(policy)


@router.put(
    "/{policy_id}",
    response_model=PolicyResponse,
    summary="Update policy",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def update_policy(
    policy_id: uuid.UUID,
    payload: PolicyUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    """Update policy attributes (Admin only)."""
    stmt = (
        select(Policy)
        .options(
            selectinload(Policy.coverages),
            selectinload(Policy.exclusions),
            selectinload(Policy.insured_items),
        )
        .where(Policy.id == policy_id)
    )
    policy = db.scalars(stmt).first()
    if not policy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy with ID '{policy_id}' not found",
        )

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(policy, field, value)

    db.flush()

    write_audit_log(
        db=db,
        action="UPDATE_POLICY",
        entity_type="Policy",
        entity_id=str(policy.id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={"updated_fields": list(update_data.keys())},
    )

    return PolicyResponse.model_validate(policy)

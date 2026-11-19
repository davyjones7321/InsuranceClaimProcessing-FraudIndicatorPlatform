"""Customer master data router with RBAC and object-level authorization."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.db.models import Customer, User
from app.db.session import get_db
from app.domain.enums import UserRole
from app.schemas.master_data import CustomerCreate, CustomerResponse, CustomerUpdate
from app.security.rbac import PermissionDeniedError, get_current_user, require_roles

router = APIRouter(prefix="/api/customers", tags=["Customers"])


@router.post(
    "",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create customer",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def create_customer(
    payload: CustomerCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerResponse:
    """Create a new customer (Admin only)."""
    # Check customer_no uniqueness
    existing = db.scalars(
        select(Customer).where(Customer.customer_no == payload.customer_no)
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Customer with customer_no '{payload.customer_no}' already exists",
        )

    customer = Customer(
        id=uuid.uuid4(),
        customer_no=payload.customer_no,
        full_name=payload.full_name,
        dob=payload.dob,
        email=payload.email,
        phone=payload.phone,
        address=payload.address,
        state_region=payload.state_region,
        source_system=payload.source_system,
        external_ref=payload.external_ref,
    )
    db.add(customer)
    db.flush()

    write_audit_log(
        db=db,
        action="CREATE_CUSTOMER",
        entity_type="Customer",
        entity_id=str(customer.id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={"customer_no": customer.customer_no, "email": customer.email},
    )

    return CustomerResponse.model_validate(customer)


@router.get(
    "",
    response_model=list[CustomerResponse],
    summary="List or search customers",
    dependencies=[require_roles(UserRole.STAFF, UserRole.MANAGER, UserRole.ADMIN)],
)
def list_customers(
    search: str | None = Query(
        default=None, description="Search by customer_no, full_name, or email"
    ),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[CustomerResponse]:
    """List customers with search and pagination (Staff, Manager, Admin)."""
    stmt = select(Customer)
    if search:
        search_filter = f"%{search.strip()}%"
        stmt = stmt.where(
            (Customer.customer_no.ilike(search_filter))
            | (Customer.full_name.ilike(search_filter))
            | (Customer.email.ilike(search_filter))
        )
    stmt = stmt.order_by(Customer.created_at.desc()).limit(limit).offset(offset)
    customers = list(db.scalars(stmt).all())
    return [CustomerResponse.model_validate(c) for c in customers]


@router.get(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Get customer profile",
)
def get_customer(
    customer_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerResponse:
    """Get customer details. Customers can view only their own record."""
    if current_user.role == UserRole.CUSTOMER:
        if current_user.customer_id != customer_id:
            raise PermissionDeniedError("Customers can view only their own profile")

    customer = db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID '{customer_id}' not found",
        )
    return CustomerResponse.model_validate(customer)


@router.put(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Update customer",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerResponse:
    """Update customer master data (Admin only)."""
    customer = db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID '{customer_id}' not found",
        )

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(customer, field, value)

    db.flush()

    write_audit_log(
        db=db,
        action="UPDATE_CUSTOMER",
        entity_type="Customer",
        entity_id=str(customer.id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={"updated_fields": list(update_data.keys())},
    )

    return CustomerResponse.model_validate(customer)

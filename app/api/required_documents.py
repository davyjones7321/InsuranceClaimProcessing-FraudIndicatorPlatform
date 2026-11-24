"""Required document types configuration router with RBAC."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.db.models import RequiredDocumentType, User
from app.db.session import get_db
from app.domain.enums import ProductType, UserRole
from app.schemas.master_data import (
    RequiredDocumentTypeCreate,
    RequiredDocumentTypeResponse,
    RequiredDocumentTypeUpdate,
)
from app.security.rbac import get_current_user, require_roles

router = APIRouter(prefix="/api/required-documents", tags=["Required Documents"])


@router.post(
    "",
    response_model=RequiredDocumentTypeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create required document type",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def create_required_document_type(
    payload: RequiredDocumentTypeCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RequiredDocumentTypeResponse:
    """Create a new required document configuration (Admin only)."""
    # Check uniqueness of (product_type, claim_category, doc_code)
    stmt = select(RequiredDocumentType).where(
        RequiredDocumentType.product_type == payload.product_type,
        RequiredDocumentType.claim_category == payload.claim_category,
        RequiredDocumentType.doc_code == payload.doc_code,
    )
    existing = db.scalars(stmt).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Document type with doc_code '{payload.doc_code}' already exists for "
                f"product '{payload.product_type.value}' and category '{payload.claim_category}'"
            ),
        )

    doc_type = RequiredDocumentType(
        id=uuid.uuid4(),
        product_type=payload.product_type,
        claim_category=payload.claim_category,
        doc_code=payload.doc_code,
        name=payload.name,
        is_mandatory=payload.is_mandatory,
    )
    db.add(doc_type)
    db.flush()

    write_audit_log(
        db=db,
        action="CREATE_REQUIRED_DOCUMENT_TYPE",
        entity_type="RequiredDocumentType",
        entity_id=str(doc_type.id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={
            "doc_code": doc_type.doc_code,
            "product_type": doc_type.product_type.value,
            "claim_category": doc_type.claim_category,
            "is_mandatory": doc_type.is_mandatory,
        },
    )

    return RequiredDocumentTypeResponse.model_validate(doc_type)


@router.get(
    "",
    response_model=list[RequiredDocumentTypeResponse],
    summary="List required document types",
    dependencies=[
        require_roles(
            UserRole.CUSTOMER,
            UserRole.STAFF,
            UserRole.ASSESSOR,
            UserRole.INVESTIGATOR,
            UserRole.MANAGER,
            UserRole.ADMIN,
        )
    ],
)
def list_required_document_types(
    product_type: ProductType | None = Query(default=None, description="Filter by product type"),
    claim_category: str | None = Query(default=None, description="Filter by claim category"),
    db: Session = Depends(get_db),
) -> list[RequiredDocumentTypeResponse]:
    """List required document types for claims (All authenticated roles)."""
    stmt = select(RequiredDocumentType)
    if product_type:
        stmt = stmt.where(RequiredDocumentType.product_type == product_type)
    if claim_category is not None:
        stmt = stmt.where(
            (RequiredDocumentType.claim_category == claim_category)
            | (RequiredDocumentType.claim_category.is_(None))
        )
    stmt = stmt.order_by(
        RequiredDocumentType.product_type.asc(), RequiredDocumentType.doc_code.asc()
    )
    doc_types = list(db.scalars(stmt).all())
    return [RequiredDocumentTypeResponse.model_validate(dt) for dt in doc_types]


@router.put(
    "/{doc_type_id}",
    response_model=RequiredDocumentTypeResponse,
    summary="Update required document type",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def update_required_document_type(
    doc_type_id: uuid.UUID,
    payload: RequiredDocumentTypeUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RequiredDocumentTypeResponse:
    """Update required document type metadata (Admin only)."""
    doc_type = db.get(RequiredDocumentType, doc_type_id)
    if not doc_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Required document type with ID '{doc_type_id}' not found",
        )

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(doc_type, field, value)

    db.flush()

    write_audit_log(
        db=db,
        action="UPDATE_REQUIRED_DOCUMENT_TYPE",
        entity_type="RequiredDocumentType",
        entity_id=str(doc_type.id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={"updated_fields": list(update_data.keys())},
    )

    return RequiredDocumentTypeResponse.model_validate(doc_type)


@router.delete(
    "/{doc_type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete required document type",
    dependencies=[require_roles(UserRole.ADMIN)],
)
def delete_required_document_type(
    doc_type_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Delete a required document type (Admin only)."""
    doc_type = db.get(RequiredDocumentType, doc_type_id)
    if not doc_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Required document type with ID '{doc_type_id}' not found",
        )

    db.delete(doc_type)
    db.flush()

    write_audit_log(
        db=db,
        action="DELETE_REQUIRED_DOCUMENT_TYPE",
        entity_type="RequiredDocumentType",
        entity_id=str(doc_type_id),
        actor_role=current_user.role.value,
        actor_id=current_user.id,
        event_data={"doc_code": doc_type.doc_code},
    )

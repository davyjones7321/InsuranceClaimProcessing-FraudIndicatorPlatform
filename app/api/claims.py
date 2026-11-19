"""Claims and claim document lifecycle API router."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Claim, ClaimDocument, User
from app.db.session import get_db
from app.domain.enums import ClaimStatus, UserRole
from app.domain.state_machine import InvalidStateTransitionError
from app.schemas.claims import (
    ClaimCreate,
    ClaimResponse,
    ClaimStatusHistoryResponse,
    ClaimStatusTransitionRequest,
)
from app.schemas.documents import DocumentResponse, DocumentReviewRequest
from app.schemas.validation import ValidationRunResponse
from app.security.rbac import (
    check_claim_access,
    get_current_user,
    require_roles,
)
from app.services.claim_service import (
    ClaimService,
    ClaimValidationError,
    StaleStatusVersionError,
)
from app.services.document_storage import (
    FileTooLargeError,
    UnsupportedMediaTypeError,
)
from app.services.validation_service import (
    ValidationService,
    ValidationServiceError,
)

router = APIRouter(prefix="/api/claims", tags=["Claims & Documents"])


@router.post(
    "",
    response_model=ClaimResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new claim",
    dependencies=[require_roles(UserRole.CUSTOMER, UserRole.STAFF, UserRole.ADMIN)],
)
def register_claim(
    payload: ClaimCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClaimResponse:
    """Register a new claim on an active policy (Customer or Staff)."""
    service = ClaimService(db)
    try:
        claim = service.register_claim(payload=payload, actor=current_user)
        return ClaimResponse.model_validate(claim)
    except ClaimValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "",
    response_model=list[ClaimResponse],
    status_code=status.HTTP_200_OK,
    summary="List claims with object-level scoping",
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
def list_claims(
    claim_status: ClaimStatus | None = Query(default=None, alias="status"),
    policy_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ClaimResponse]:
    """Retrieve claims with strict object-level access boundaries."""
    stmt = select(Claim)

    # Scoping by role (Section H.87)
    if current_user.role == UserRole.CUSTOMER:
        stmt = stmt.where(Claim.customer_id == current_user.customer_id)
    elif current_user.role == UserRole.STAFF:
        stmt = stmt.where(Claim.registered_by == current_user.id)
    elif current_user.role == UserRole.INVESTIGATOR:
        # Investigators see only assigned investigations
        stmt = stmt.join(Claim.investigations).where(
            Claim.investigations.any(assigned_to=current_user.id)
        )

    if claim_status:
        stmt = stmt.where(Claim.status == claim_status)
    if policy_id:
        stmt = stmt.where(Claim.policy_id == policy_id)

    stmt = stmt.order_by(Claim.registered_at.desc())
    claims = db.scalars(stmt).all()
    return [ClaimResponse.model_validate(c) for c in claims]


@router.get(
    "/{claim_id}",
    response_model=ClaimResponse,
    status_code=status.HTTP_200_OK,
    summary="Get claim details",
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
def get_claim(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClaimResponse:
    """Retrieve single claim by ID with authorization verification."""
    claim = db.get(Claim, claim_id)
    if not claim:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Claim '{claim_id}' not found"
        )
    check_claim_access(current_user, claim)
    return ClaimResponse.model_validate(claim)


@router.get(
    "/{claim_id}/history",
    response_model=list[ClaimStatusHistoryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get claim status progression history",
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
def get_claim_history(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ClaimStatusHistoryResponse]:
    """Retrieve audit status history for a claim."""
    claim = db.get(Claim, claim_id)
    if not claim:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Claim '{claim_id}' not found"
        )
    check_claim_access(current_user, claim)
    return [ClaimStatusHistoryResponse.model_validate(h) for h in claim.status_history]


@router.patch(
    "/{claim_id}/status",
    response_model=ClaimResponse,
    status_code=status.HTTP_200_OK,
    summary="Transition claim state",
    dependencies=[
        require_roles(UserRole.STAFF, UserRole.ASSESSOR, UserRole.MANAGER, UserRole.ADMIN)
    ],
)
def transition_claim_status(
    claim_id: uuid.UUID,
    payload: ClaimStatusTransitionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClaimResponse:
    """Advance claim state with optimistic locking and transition rule validation."""
    service = ClaimService(db)
    try:
        updated = service.transition_status(claim_id=claim_id, req=payload, actor=current_user)
        return ClaimResponse.model_validate(updated)
    except StaleStatusVersionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ClaimValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/{claim_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload claim document",
    dependencies=[require_roles(UserRole.CUSTOMER, UserRole.STAFF, UserRole.ADMIN)],
)
async def upload_document(
    claim_id: uuid.UUID,
    doc_code: Annotated[str, Form(min_length=2, max_length=64)],
    file: UploadFile = File(...),
    document_date: Annotated[date | None, Form()] = None,
    named_person: Annotated[str | None, Form(max_length=255)] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    """Upload and attach a supporting document to a claim with validation and SHA-256 computation."""
    content = await file.read()
    service = ClaimService(db)
    try:
        doc = service.upload_document(
            claim_id=claim_id,
            doc_code=doc_code,
            file_name=file.filename or "uploaded_document",
            content=content,
            mime_type=file.content_type or "application/octet-stream",
            actor=current_user,
            document_date=document_date,
            named_person=named_person,
        )
        return DocumentResponse.model_validate(doc)
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except UnsupportedMediaTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except ClaimValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "/{claim_id}/documents",
    response_model=list[DocumentResponse],
    status_code=status.HTTP_200_OK,
    summary="List claim documents",
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
def list_claim_documents(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DocumentResponse]:
    """Retrieve metadata of all documents attached to a claim."""
    claim = db.get(Claim, claim_id)
    if not claim:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Claim '{claim_id}' not found"
        )
    check_claim_access(current_user, claim)

    docs = db.scalars(
        select(ClaimDocument)
        .where(ClaimDocument.claim_id == claim_id)
        .order_by(ClaimDocument.created_at.desc())
    ).all()
    return [DocumentResponse.model_validate(d) for d in docs]


@router.get(
    "/{claim_id}/documents/{document_id}/download",
    status_code=status.HTTP_200_OK,
    summary="Download claim document binary",
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
def download_claim_document(
    claim_id: uuid.UUID,
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Download document binary with access verification and sensitive document view auditing."""
    service = ClaimService(db)
    try:
        doc, content = service.get_document_for_download(
            claim_id=claim_id,
            document_id=document_id,
            actor=current_user,
        )
        return Response(
            content=content,
            media_type=doc.mime_type,
            headers={
                "Content-Disposition": f'attachment; filename="{doc.file_name}"',
                "Content-Length": str(doc.size_bytes),
                "X-Document-SHA256": doc.sha256,
            },
        )
    except ClaimValidationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.patch(
    "/{claim_id}/documents/{document_id}/review",
    response_model=DocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Review claim document",
    dependencies=[
        require_roles(UserRole.ASSESSOR, UserRole.STAFF, UserRole.MANAGER, UserRole.ADMIN)
    ],
)
def review_claim_document(
    claim_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: DocumentReviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    """Review claim document setting status to VALID or INVALID with mandatory remarks."""
    service = ClaimService(db)
    try:
        doc = service.review_document(
            claim_id=claim_id,
            document_id=document_id,
            review_req=payload,
            actor=current_user,
        )
        return DocumentResponse.model_validate(doc)
    except ClaimValidationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/{claim_id}/validate",
    response_model=ValidationRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute deterministic validation engine against claim",
    dependencies=[
        require_roles(
            UserRole.CUSTOMER,
            UserRole.STAFF,
            UserRole.ASSESSOR,
            UserRole.MANAGER,
            UserRole.ADMIN,
        )
    ],
)
def validate_claim(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ValidationRunResponse:
    """Evaluate pure validation rules V01-V13 deterministically and persist execution run."""
    service = ValidationService(db)
    try:
        run = service.validate_claim(claim_id=claim_id, actor=current_user)
        return ValidationRunResponse.model_validate(run)
    except ValidationServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "/{claim_id}/validation-runs",
    response_model=list[ValidationRunResponse],
    status_code=status.HTTP_200_OK,
    summary="List all validation runs for a claim",
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
def list_validation_runs(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ValidationRunResponse]:
    """Retrieve full history of validation runs for a claim."""
    service = ValidationService(db)
    try:
        runs = service.get_validation_runs(claim_id=claim_id, actor=current_user)
        return [ValidationRunResponse.model_validate(r) for r in runs]
    except ValidationServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/{claim_id}/validation-runs/latest",
    response_model=ValidationRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Get latest validation run for a claim",
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
def get_latest_validation_run(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ValidationRunResponse:
    """Retrieve the most recent validation run for a claim."""
    service = ValidationService(db)
    try:
        run = service.get_latest_validation_run(claim_id=claim_id, actor=current_user)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No validation runs recorded for claim '{claim_id}'",
            )
        return ValidationRunResponse.model_validate(run)
    except ValidationServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

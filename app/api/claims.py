"""Claims and claim document lifecycle API router."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    Claim,
    ClaimDocument,
    Customer,
    Policy,
    RequiredDocumentType,
    User,
)
from app.db.session import get_db
from app.domain.enums import ClaimStatus, UserRole
from app.domain.state_machine import InvalidStateTransitionError
from app.schemas.assessments import AssessmentCreate, AssessmentResponse
from app.schemas.claims import (
    ClaimCreate,
    ClaimReopenRequest,
    ClaimResponse,
    ClaimStatusHistoryResponse,
    ClaimStatusTransitionRequest,
)
from app.schemas.decisions import DecisionCreate, DecisionResponse
from app.schemas.documents import (
    DocumentResponse,
    DocumentReviewRequest,
    IdentityAttestationCreate,
    IdentityAttestationResponse,
)
from app.schemas.investigations import (
    InvestigationAssignRequest,
    InvestigationCompleteRequest,
    InvestigationEscalateRequest,
    InvestigationFindingCreate,
    InvestigationFindingResponse,
    InvestigationResponse,
)
from app.schemas.risk import RiskEvaluationResponse
from app.schemas.validation import ValidationRunResponse
from app.security.access_log import log_access
from app.security.rbac import (
    check_claim_access,
    get_current_user,
    require_roles,
)
from app.services.assessment_service import (
    AssessmentPermissionError,
    AssessmentService,
    AssessmentServiceError,
    AssessmentValidationError,
)
from app.services.claim_service import (
    ClaimService,
    ClaimValidationError,
    StaleStatusVersionError,
)
from app.services.decision_service import (
    DecisionPermissionError,
    DecisionService,
    DecisionServiceError,
    DecisionValidationError,
)
from app.services.document_storage import (
    FileTooLargeError,
    UnsupportedMediaTypeError,
)
from app.services.investigation_service import (
    InvestigationPermissionError,
    InvestigationService,
    InvestigationServiceError,
    InvestigationValidationError,
)
from app.services.risk_service import (
    RiskService,
    RiskServiceError,
)
from app.services.validation_service import (
    ValidationService,
    ValidationServiceError,
)

router = APIRouter(prefix="/api/claims", tags=["Claims & Documents"])


def serialize_claim_for_user(claim: Claim, user: User) -> ClaimResponse:
    """Format claim representation with customer investigation masking (P1 task 2, D6)."""
    resp = ClaimResponse.model_validate(claim)
    if user.role == UserRole.CUSTOMER and resp.status in (
        ClaimStatus.UNDER_INVESTIGATION,
        ClaimStatus.ESCALATED,
        "UNDER_INVESTIGATION",
        "ESCALATED",
    ):
        resp.status = "UNDER_REVIEW"
    return resp


def serialize_history_for_user(history: list[Any], user: User) -> list[ClaimStatusHistoryResponse]:
    """Format status history with investigation secrecy for customers (D6)."""
    results: list[ClaimStatusHistoryResponse] = []
    for h in history:
        item = ClaimStatusHistoryResponse.model_validate(h)
        if user.role == UserRole.CUSTOMER:
            if item.from_status in ("UNDER_INVESTIGATION", "ESCALATED"):
                item.from_status = "UNDER_REVIEW"
            if item.to_status in ("UNDER_INVESTIGATION", "ESCALATED"):
                item.to_status = "UNDER_REVIEW"
            if item.reason and (
                "investigat" in item.reason.lower() or "escalat" in item.reason.lower()
            ):
                item.reason = "Claim under review"
        results.append(item)
    return results


def serialize_risk_evaluation_for_user(eval_run: Any, user: User) -> RiskEvaluationResponse:
    """Filter risk details per Decision D6."""
    resp = RiskEvaluationResponse.model_validate(eval_run)
    if user.role == UserRole.ASSESSOR:
        # D6: Assessor gets category and completeness only; no indicator evidence
        resp.indicator_results = []
        resp.inputs_snapshot = {}
        resp.score = 0
    return resp


def serialize_document_for_api(
    doc: ClaimDocument, db: Session, claim: Claim | None = None
) -> DocumentResponse:
    """Serialize ClaimDocument with is_identity_bearing and latest_attestation metadata."""
    if claim is None:
        claim = db.get(Claim, doc.claim_id)
    policy = db.get(Policy, claim.policy_id) if claim else None
    customer = db.get(Customer, claim.customer_id) if claim else None

    is_id_bearing = False
    if policy:
        req_doc = db.scalars(
            select(RequiredDocumentType).where(
                RequiredDocumentType.product_type == policy.product_type,
                RequiredDocumentType.doc_code == doc.doc_code,
            )
        ).first()
        if req_doc and req_doc.is_identity_bearing:
            is_id_bearing = True

    latest_att = max(doc.attestations, key=lambda a: a.rev) if doc.attestations else None
    att_resp: IdentityAttestationResponse | None = None
    if latest_att:
        is_stale = (
            latest_att.subject_identity_version != customer.identity_version if customer else False
        )
        att_resp = IdentityAttestationResponse(
            document_id=latest_att.document_id,
            rev=latest_att.rev,
            prev_rev=latest_att.prev_rev,
            document_sha256=latest_att.document_sha256,
            result=latest_att.result,
            reason_code=latest_att.reason_code,
            remarks=latest_att.remarks,
            reviewer_id=latest_att.reviewer_id,
            reviewer_role=latest_att.reviewer_role,
            subject_identity_version=latest_att.subject_identity_version,
            is_current=True,
            is_stale=is_stale,
            created_at=latest_att.created_at,
        )

    return DocumentResponse(
        id=doc.id,
        claim_id=doc.claim_id,
        doc_code=doc.doc_code,
        file_name=doc.file_name,
        sha256=doc.sha256,
        size_bytes=doc.size_bytes,
        mime_type=doc.mime_type,
        document_date=doc.document_date,
        named_person=doc.named_person,
        status=doc.status,
        reason_code=doc.reason_code,
        review_remarks=doc.review_remarks,
        reviewed_by=doc.reviewed_by,
        reviewed_at=doc.reviewed_at,
        uploaded_by=doc.uploaded_by,
        created_at=doc.created_at,
        is_identity_bearing=is_id_bearing,
        latest_attestation=att_resp,
    )


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
        return serialize_claim_for_user(claim, current_user)
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

    # Scoping by role (Section H.87, D6, D7)
    if current_user.role == UserRole.CUSTOMER:
        stmt = stmt.where(Claim.customer_id == current_user.customer_id)
    elif current_user.role == UserRole.STAFF:
        # D7: Staff see summary for all claims
        pass
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
    return [serialize_claim_for_user(c, current_user) for c in claims]


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
    check_claim_access(current_user, claim, action="READ_DETAIL")

    # Record access log for non-owner sensitive record views (P1 task 5)
    if claim.customer_id != current_user.customer_id:
        log_access(
            db=db,
            actor=current_user,
            claim_id=claim.id,
            resource_type="CLAIM_DETAIL",
            action="VIEW",
        )

    return serialize_claim_for_user(claim, current_user)


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
    check_claim_access(current_user, claim, action="READ")
    return serialize_history_for_user(list(claim.status_history), current_user)


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
        return serialize_claim_for_user(updated, current_user)
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
    dependencies=[
        require_roles(
            UserRole.CUSTOMER,
            UserRole.STAFF,
            UserRole.MANAGER,
            UserRole.ADMIN,
        )
    ],
)
async def upload_document(
    claim_id: uuid.UUID,
    request: Request,
    doc_code: Annotated[str, Form(min_length=2, max_length=64)],
    file: UploadFile = File(...),
    document_date: Annotated[date | None, Form()] = None,
    named_person: Annotated[str | None, Form(max_length=255)] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    """Upload and attach a supporting document to a claim with validation and SHA-256 computation."""
    # Enforce size while reading per P1 task 6
    content_length_header = request.headers.get("Content-Length")
    if content_length_header:
        try:
            cl = int(content_length_header)
            if cl > settings.UPLOAD_MAX_SIZE_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Content-Length {cl} exceeds maximum allowed limit of {settings.UPLOAD_MAX_SIZE_BYTES} bytes",
                )
        except ValueError:
            pass

    content_chunks: list[bytes] = []
    total_bytes = 0
    chunk_size = 65536
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > settings.UPLOAD_MAX_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File size exceeds maximum allowed limit of {settings.UPLOAD_MAX_SIZE_BYTES} bytes",
            )
        content_chunks.append(chunk)

    content = b"".join(content_chunks)
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
        return serialize_document_for_api(doc, db)
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
    return [serialize_document_for_api(d, db, claim) for d in docs]


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
    dependencies=[require_roles(UserRole.ASSESSOR, UserRole.MANAGER)],
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
        return serialize_document_for_api(doc, db)
    except StaleStatusVersionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ClaimValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/{claim_id}/documents/{document_id}/attestations",
    response_model=IdentityAttestationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit identity attestation revision for document",
    dependencies=[require_roles(UserRole.ASSESSOR, UserRole.MANAGER)],
)
def attest_claim_document_identity(
    claim_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: IdentityAttestationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IdentityAttestationResponse:
    """Submit append-only identity attestation revision (Decision D4)."""
    service = ClaimService(db)
    try:
        att = service.attest_document_identity(
            claim_id=claim_id,
            document_id=document_id,
            payload=payload,
            actor=current_user,
        )
        claim = db.get(Claim, claim_id)
        customer = db.get(Customer, claim.customer_id) if claim else None
        is_stale = att.subject_identity_version != customer.identity_version if customer else False
        return IdentityAttestationResponse(
            document_id=att.document_id,
            rev=att.rev,
            prev_rev=att.prev_rev,
            document_sha256=att.document_sha256,
            result=att.result,
            reason_code=att.reason_code,
            remarks=att.remarks,
            reviewer_id=att.reviewer_id,
            reviewer_role=att.reviewer_role,
            subject_identity_version=att.subject_identity_version,
            is_current=True,
            is_stale=is_stale,
            created_at=att.created_at,
        )
    except StaleStatusVersionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ClaimValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "/{claim_id}/documents/{document_id}/attestations",
    response_model=list[IdentityAttestationResponse],
    status_code=status.HTTP_200_OK,
    summary="List identity attestation revisions for document",
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
def list_claim_document_attestations(
    claim_id: uuid.UUID,
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[IdentityAttestationResponse]:
    """List append-only identity attestations for a document in revision order."""
    service = ClaimService(db)
    try:
        attestations = service.get_document_attestations(
            claim_id=claim_id,
            document_id=document_id,
            actor=current_user,
        )
        claim = db.get(Claim, claim_id)
        customer = db.get(Customer, claim.customer_id) if claim else None
        latest_rev = max((a.rev for a in attestations), default=0)
        results: list[IdentityAttestationResponse] = []
        for att in attestations:
            is_cur = att.rev == latest_rev
            is_stale = (
                att.subject_identity_version != customer.identity_version if customer else False
            )
            results.append(
                IdentityAttestationResponse(
                    document_id=att.document_id,
                    rev=att.rev,
                    prev_rev=att.prev_rev,
                    document_sha256=att.document_sha256,
                    result=att.result,
                    reason_code=att.reason_code,
                    remarks=att.remarks,
                    reviewer_id=att.reviewer_id,
                    reviewer_role=att.reviewer_role,
                    subject_identity_version=att.subject_identity_version,
                    is_current=is_cur,
                    is_stale=is_stale,
                    created_at=att.created_at,
                )
            )
        return results
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


@router.post(
    "/{claim_id}/evaluate-risk",
    response_model=RiskEvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute deterministic risk indicator engine against validated claim",
    dependencies=[
        require_roles(
            UserRole.ASSESSOR,
            UserRole.INVESTIGATOR,
            UserRole.MANAGER,
            UserRole.ADMIN,
        )
    ],
)
def evaluate_claim_risk(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RiskEvaluationResponse:
    """Evaluate pure risk indicators R01-R10 and determine investigation routing."""
    service = RiskService(db)
    try:
        eval_run = service.evaluate_claim_risk(claim_id=claim_id, actor=current_user)
        return serialize_risk_evaluation_for_user(eval_run, current_user)
    except RiskServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "/{claim_id}/risk-evaluations",
    response_model=list[RiskEvaluationResponse],
    status_code=status.HTTP_200_OK,
    summary="List all risk evaluations for a claim",
    dependencies=[
        require_roles(
            UserRole.ASSESSOR,
            UserRole.INVESTIGATOR,
            UserRole.MANAGER,
            UserRole.ADMIN,
        )
    ],
)
def list_risk_evaluations(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RiskEvaluationResponse]:
    """Retrieve history of risk evaluations for a claim."""
    service = RiskService(db)
    try:
        evals = service.get_risk_evaluations(claim_id=claim_id, actor=current_user)
        return [serialize_risk_evaluation_for_user(e, current_user) for e in evals]
    except RiskServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/{claim_id}/risk-evaluations/latest",
    response_model=RiskEvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get latest risk evaluation for a claim",
    dependencies=[
        require_roles(
            UserRole.ASSESSOR,
            UserRole.INVESTIGATOR,
            UserRole.MANAGER,
            UserRole.ADMIN,
        )
    ],
)
def get_latest_risk_evaluation(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RiskEvaluationResponse:
    """Retrieve the most recent risk evaluation for a claim."""
    service = RiskService(db)
    try:
        eval_run = service.get_latest_risk_evaluation(claim_id=claim_id, actor=current_user)
        if not eval_run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No risk evaluations recorded for claim '{claim_id}'",
            )
        return serialize_risk_evaluation_for_user(eval_run, current_user)
    except RiskServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Assessment endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{claim_id}/assessments",
    response_model=AssessmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record assessment calculation and eligible amount",
    dependencies=[require_roles(UserRole.ASSESSOR, UserRole.MANAGER)],
)
def create_assessment(
    claim_id: uuid.UUID,
    payload: AssessmentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AssessmentResponse:
    """Evaluate engine cap, verify separation of duties, and advance claim to ASSESSED."""
    service = AssessmentService(db)
    try:
        assessment = service.create_assessment(
            claim_id=claim_id,
            payload=payload,
            actor=current_user,
        )
        return AssessmentResponse.model_validate(assessment)
    except AssessmentValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AssessmentPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except AssessmentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/{claim_id}/assessments",
    response_model=list[AssessmentResponse],
    status_code=status.HTTP_200_OK,
    summary="List assessments for a claim",
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
def list_assessments(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AssessmentResponse]:
    """Retrieve history of assessments for a claim."""
    service = AssessmentService(db)
    try:
        assessments = service.get_assessments(claim_id=claim_id, actor=current_user)
        return [AssessmentResponse.model_validate(a) for a in assessments]
    except AssessmentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Investigation endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{claim_id}/investigations",
    response_model=list[InvestigationResponse],
    status_code=status.HTTP_200_OK,
    summary="List investigations and findings for a claim",
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
def list_investigations(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[InvestigationResponse]:
    """Retrieve all investigations and findings for a claim."""
    service = InvestigationService(db)
    try:
        investigations = service.get_investigations(claim_id=claim_id, actor=current_user)
        return [InvestigationResponse.model_validate(inv) for inv in investigations]
    except InvestigationServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/{claim_id}/investigations/assign",
    response_model=InvestigationResponse,
    status_code=status.HTTP_200_OK,
    summary="Assign an investigator to an investigation",
    dependencies=[require_roles(UserRole.MANAGER, UserRole.ADMIN)],
)
def assign_investigator(
    claim_id: uuid.UUID,
    payload: InvestigationAssignRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvestigationResponse:
    """Assign an investigator (Manager or Admin only)."""
    service = InvestigationService(db)
    try:
        inv = service.assign_investigator(
            claim_id=claim_id,
            investigator_id=payload.investigator_id,
            actor=current_user,
        )
        return InvestigationResponse.model_validate(inv)
    except InvestigationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvestigationPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InvestigationServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/{claim_id}/investigations/review",
    response_model=InvestigationResponse,
    status_code=status.HTTP_200_OK,
    summary="Start reviewing an active investigation",
    dependencies=[require_roles(UserRole.INVESTIGATOR, UserRole.MANAGER, UserRole.ADMIN)],
)
def start_investigation_review(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvestigationResponse:
    """Transition investigation to UNDER_REVIEW."""
    service = InvestigationService(db)
    try:
        inv = service.start_review(claim_id=claim_id, actor=current_user)
        return InvestigationResponse.model_validate(inv)
    except InvestigationPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InvestigationServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/{claim_id}/investigations/findings",
    response_model=InvestigationFindingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record an append-only finding under an active investigation",
    dependencies=[require_roles(UserRole.INVESTIGATOR, UserRole.MANAGER, UserRole.ADMIN)],
)
def add_investigation_finding(
    claim_id: uuid.UUID,
    payload: InvestigationFindingCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvestigationFindingResponse:
    """Add a permanent finding to an open investigation."""
    service = InvestigationService(db)
    try:
        finding = service.add_finding(
            claim_id=claim_id,
            finding_text=payload.finding_text,
            actor=current_user,
        )
        return InvestigationFindingResponse.model_validate(finding)
    except InvestigationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvestigationPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InvestigationServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/{claim_id}/investigations/escalate",
    response_model=InvestigationResponse,
    status_code=status.HTTP_200_OK,
    summary="Escalate an investigation to management",
    dependencies=[require_roles(UserRole.INVESTIGATOR, UserRole.MANAGER, UserRole.ADMIN)],
)
def escalate_investigation(
    claim_id: uuid.UUID,
    payload: InvestigationEscalateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvestigationResponse:
    """Escalate investigation and advance claim to ESCALATED status."""
    service = InvestigationService(db)
    try:
        inv = service.escalate_investigation(
            claim_id=claim_id,
            reason=payload.reason,
            actor=current_user,
        )
        return InvestigationResponse.model_validate(inv)
    except InvestigationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvestigationPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InvestigationServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/{claim_id}/investigations/complete",
    response_model=InvestigationResponse,
    status_code=status.HTTP_200_OK,
    summary="Conclude investigation with outcome and advance claim to READY_FOR_ASSESSMENT",
    dependencies=[require_roles(UserRole.INVESTIGATOR, UserRole.MANAGER, UserRole.ADMIN)],
)
def complete_investigation(
    claim_id: uuid.UUID,
    payload: InvestigationCompleteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InvestigationResponse:
    """Complete an investigation and route claim to assessment queue."""
    service = InvestigationService(db)
    try:
        inv = service.complete_investigation(
            claim_id=claim_id,
            outcome=payload.outcome,
            actor=current_user,
            remarks=payload.remarks,
        )
        return InvestigationResponse.model_validate(inv)
    except InvestigationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvestigationPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InvestigationServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Decision endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{claim_id}/decisions",
    response_model=DecisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a definitive claim decision (APPROVED, REJECTED, INFO_REQUESTED)",
    dependencies=[require_roles(UserRole.STAFF, UserRole.MANAGER)],
)
def make_claim_decision(
    claim_id: uuid.UUID,
    payload: DecisionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DecisionResponse:
    """Execute a claim decision with separation of duties, threshold checks, and coverage ledger updates."""
    service = DecisionService(db)
    try:
        decision = service.make_decision(
            claim_id=claim_id,
            payload=payload,
            actor=current_user,
        )
        return DecisionResponse.model_validate(decision)
    except StaleStatusVersionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DecisionValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except DecisionPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except DecisionServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/{claim_id}/reopen",
    response_model=ClaimResponse,
    status_code=status.HTTP_200_OK,
    summary="Reopen an approved or rejected claim (Manager only)",
    dependencies=[require_roles(UserRole.MANAGER, UserRole.ADMIN)],
)
def reopen_claim(
    claim_id: uuid.UUID,
    payload: ClaimReopenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClaimResponse:
    """Manager-only reopen of an APPROVED or REJECTED claim to READY_FOR_ASSESSMENT with compensating reversal."""
    service = DecisionService(db)
    try:
        claim = service.reopen_claim(
            claim_id=claim_id,
            actor=current_user,
            remarks=payload.remarks,
            expected_status_version=payload.expected_status_version,
        )
        return ClaimResponse.model_validate(claim)
    except StaleStatusVersionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DecisionValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except DecisionPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except DecisionServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/{claim_id}/decisions",
    response_model=list[DecisionResponse],
    status_code=status.HTTP_200_OK,
    summary="List decisions for a claim",
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
def list_decisions(
    claim_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DecisionResponse]:
    """Retrieve history of decisions for a claim."""
    service = DecisionService(db)
    try:
        decisions = service.get_decisions(claim_id=claim_id, actor=current_user)
        return [DecisionResponse.model_validate(d) for d in decisions]
    except DecisionServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

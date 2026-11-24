"""Server-rendered Jinja2 web UI routes per Decision D2 (Phase P8 Task 1).

Features:
- Role-based authorization on every view and action
- Customer-safe status masking (UNDER_INVESTIGATION -> UNDER_REVIEW)
- Zero risk data leakage to unauthorized roles (Customer and Staff)
- Assessor visibility restricted to category and completeness only
- Investigator and Manager visibility with mandatory pilot disclaimer banner
- Separation of duties enforcement on assessment, investigation, and decisions
"""

import secrets
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.config import settings
from app.db.models import (
    Assessment,
    Claim,
    ClaimDocument,
    Coverage,
    Customer,
    Decision,
    Policy,
    User,
)
from app.db.session import get_db
from app.domain.enums import (
    ClaimStatus,
    DecisionType,
    DocumentStatus,
    InvestigationOutcome,
    LimitScope,
    PolicyStatus,
    UserRole,
)
from app.engine.assessment_cap import calculate_assessment_cap
from app.engine.types import AssessmentInput
from app.schemas.assessments import AssessmentCreate
from app.schemas.auth import RegistrationRequest
from app.schemas.claims import ClaimCreate
from app.schemas.decisions import DecisionCreate
from app.schemas.documents import DocumentReviewRequest
from app.security.csrf import generate_csrf_token, validate_csrf_token
from app.security.password import verify_password
from app.security.rate_limit import AccountLockedError, check_rate_limit, record_login_attempt
from app.security.rbac import (
    PermissionDeniedError,
    check_claim_access,
    get_customer_safe_status,
)
from app.security.session import create_session, delete_session, get_user_from_session
from app.services.assessment_service import AssessmentService
from app.services.claim_service import ClaimService, ClaimValidationError
from app.services.decision_service import DecisionPermissionError, DecisionService
from app.services.investigation_service import InvestigationService
from app.services.registration_service import RegistrationConflictError, register_customer
from app.services.risk_service import RiskService

router = APIRouter(prefix="/ui", tags=["Web UI"])
templates = Jinja2Templates(directory="templates")


def get_ui_session(
    request: Request, db: Session = Depends(get_db)
) -> tuple[User | None, str | None, str]:
    """Extract authenticated user, session ID, and CSRF token from session cookie."""
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    user = get_user_from_session(db, session_id)
    csrf_token = generate_csrf_token(session_id) if session_id else ""
    return user, session_id, csrf_token


# ==============================================================================
# Authentication Views
# ==============================================================================


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    db: Session = Depends(get_db),
    msg: str | None = None,
    msg_type: str = "info",
) -> Response:
    """Render user sign-in page."""
    user, _, _ = get_ui_session(request, db)
    if user:
        return RedirectResponse(url="/ui/claims", status_code=303)

    login_csrf_id = request.cookies.get("claim_login_csrf") or secrets.token_urlsafe(32)
    login_csrf_token = generate_csrf_token(login_csrf_id)
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "user": None,
            "csrf_token": login_csrf_token,
            "alert_message": msg,
            "alert_type": msg_type,
        },
    )
    response.set_cookie(
        key="claim_login_csrf",
        value=login_csrf_id,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.ENV in ("staging", "production"),
        path="/ui",
    )
    return response


@router.post("/login")
def login_action(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Authenticate credentials using the same controls as the API login."""
    login_csrf_id = request.cookies.get("claim_login_csrf")
    if not login_csrf_id or not validate_csrf_token(login_csrf_id, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    identifier = email.strip().lower()
    client_ip = request.client.host if request.client else "unknown"

    try:
        check_rate_limit(db, identifier)
    except AccountLockedError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    user = db.scalars(select(User).where(User.email == identifier)).first()
    if not user or not user.is_active or not verify_password(user.password_hash, password):
        record_login_attempt(db, identifier, success=False)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "user": None,
                "csrf_token": generate_csrf_token(login_csrf_id),
                "alert_message": "Invalid email or password",
                "alert_type": "error",
            },
            status_code=400,
        )

    record_login_attempt(db, identifier, success=True)

    # Rotate any existing browser session before issuing the authenticated session.
    existing_session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if existing_session_id:
        delete_session(db, existing_session_id)

    session_id = create_session(db, user)
    write_audit_log(
        db=db,
        action="USER_LOGIN",
        entity_type="User",
        entity_id=str(user.id),
        actor_role=user.role.value,
        actor_id=user.id,
        event_data={"user_id": str(user.id), "role": user.role.value, "channel": "WEB"},
        ip=client_ip,
    )
    db.commit()

    response = RedirectResponse(url="/ui/claims", status_code=303)
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=session_id,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.ENV in ("staging", "production"),
    )
    response.delete_cookie(key="claim_login_csrf", path="/ui")
    return response


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)) -> Response:
    """Render the public customer registration page."""
    user, _, _ = get_ui_session(request, db)
    if user:
        return RedirectResponse(url="/ui/claims", status_code=303)

    csrf_id = request.cookies.get("claim_login_csrf") or secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "request": request,
            "user": None,
            "csrf_token": generate_csrf_token(csrf_id),
            "today": date.today().isoformat(),
        },
    )
    response.set_cookie(
        key="claim_login_csrf",
        value=csrf_id,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.ENV in ("staging", "production"),
        path="/ui",
    )
    return response


@router.post("/register")
def register_action(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    dob: str = Form(...),
    phone: str = Form(...),
    address: str = Form(...),
    state_region: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Create a customer account, then establish its browser session."""
    csrf_id = request.cookies.get("claim_login_csrf")
    if not csrf_id or not validate_csrf_token(csrf_id, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    if password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "request": request,
                "user": None,
                "csrf_token": generate_csrf_token(csrf_id),
                "today": date.today().isoformat(),
                "alert_message": "Passwords do not match",
                "alert_type": "error",
            },
            status_code=400,
        )

    try:
        payload = RegistrationRequest(
            full_name=full_name,
            email=email,
            password=password,
            dob=dob,
            phone=phone,
            address=address,
            state_region=state_region,
        )
        user = register_customer(db, payload)
        session_id = create_session(db, user)
        db.commit()
    except RegistrationConflictError as exc:
        db.rollback()
        message = str(exc)
    except (ValidationError, ValueError) as exc:
        db.rollback()
        message = (
            "; ".join(error["msg"] for error in exc.errors())
            if isinstance(exc, ValidationError)
            else str(exc)
        )
    else:
        response = RedirectResponse(url="/ui/claims", status_code=303)
        response.set_cookie(
            key=settings.SESSION_COOKIE_NAME,
            value=session_id,
            max_age=settings.SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=settings.ENV in ("staging", "production"),
        )
        response.delete_cookie(key="claim_login_csrf", path="/ui")
        return response

    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "request": request,
            "user": None,
            "csrf_token": generate_csrf_token(csrf_id),
            "today": date.today().isoformat(),
            "alert_message": message,
            "alert_type": "error",
        },
        status_code=400,
    )


@router.post("/logout")
def logout_action(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Terminate user session after validating the session-bound CSRF token."""
    _, session_id, _ = get_ui_session(request, db)
    if not session_id or not validate_csrf_token(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    delete_session(db, session_id)
    db.commit()

    response = RedirectResponse(
        url="/ui/login?msg=Logged+out+successfully&msg_type=success", status_code=303
    )
    response.delete_cookie(key=settings.SESSION_COOKIE_NAME)
    return response


# ==============================================================================
# Claims Portfolio & Details
# ==============================================================================


@router.get("", response_class=HTMLResponse)
@router.get("/claims", response_class=HTMLResponse)
def claims_list(
    request: Request,
    db: Session = Depends(get_db),
    msg: str | None = None,
    msg_type: str = "info",
) -> Response:
    """Render role-specific claims portfolio list."""
    user, session_id, csrf_token = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)

    query = select(Claim).order_by(Claim.registered_at.desc())

    # Role-based scoping
    if user.role == UserRole.CUSTOMER:
        query = query.where(Claim.customer_id == user.customer_id)
    elif user.role == UserRole.ASSESSOR:
        query = query.where(
            Claim.status.in_(
                [
                    ClaimStatus.READY_FOR_ASSESSMENT,
                    ClaimStatus.ASSESSED,
                    ClaimStatus.APPROVED,
                ]
            )
        )
    elif user.role == UserRole.INVESTIGATOR:
        query = query.where(
            Claim.status.in_(
                [
                    ClaimStatus.UNDER_INVESTIGATION,
                    ClaimStatus.ESCALATED,
                    ClaimStatus.INVESTIGATION_COMPLETE,
                ]
            )
        )

    claims_raw = db.scalars(query).all()

    # Build customer-safe display rows
    claims_view: list[dict[str, Any]] = []
    for c in claims_raw:
        pol = db.get(Policy, c.policy_id)
        display_status = (
            get_customer_safe_status(c.status) if user.role == UserRole.CUSTOMER else c.status.value
        )
        claims_view.append(
            {
                "id": str(c.id),
                "claim_ref": c.claim_ref,
                "policy_no": pol.policy_no if pol else "N/A",
                "incident_date": c.incident_date.isoformat(),
                "claim_category": c.claim_category,
                "requested_amount": c.requested_amount,
                "display_status": display_status,
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="claims_list.html",
        context={
            "request": request,
            "user": user,
            "csrf_token": csrf_token,
            "claims": claims_view,
            "active_tab": "claims",
            "alert_message": msg,
            "alert_type": msg_type,
        },
    )


@router.get("/claims/new", response_class=HTMLResponse)
def claim_register_page(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Render claim registration form."""
    user, session_id, csrf_token = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if user.role not in (UserRole.CUSTOMER, UserRole.STAFF):
        raise HTTPException(status_code=403, detail="Only customers and staff may register claims")

    pol_query = select(Policy).where(Policy.status == PolicyStatus.ACTIVE)
    if user.role == UserRole.CUSTOMER:
        pol_query = pol_query.where(Policy.customer_id == user.customer_id)

    available_policies = db.scalars(pol_query).all()

    return templates.TemplateResponse(
        request=request,
        name="claim_register.html",
        context={
            "request": request,
            "user": user,
            "csrf_token": csrf_token,
            "available_policies": available_policies,
            "active_tab": "new_claim",
        },
    )


@router.post("/claims/new")
def claim_register_action(
    request: Request,
    policy_id: uuid.UUID = Form(...),
    claim_category: str = Form(...),
    incident_date: str = Form(...),
    requested_amount: Decimal = Form(...),
    incident_location: str = Form(...),
    incident_description: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Submit claim registration."""
    user, session_id, _ = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if not session_id or not validate_csrf_token(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    from datetime import date as dt_date

    try:
        service = ClaimService(db=db)
        claim_in = ClaimCreate(
            policy_id=policy_id,
            claim_category=claim_category,
            incident_date=dt_date.fromisoformat(incident_date),
            requested_amount=requested_amount,
            incident_location=incident_location,
            incident_description=incident_description,
        )
        new_claim = service.register_claim(claim_in, actor=user)
        db.commit()
        return RedirectResponse(
            url=f"/ui/claims/{new_claim.id}?msg=Claim+{new_claim.claim_ref}+registered+successfully&msg_type=success",
            status_code=303,
        )
    except (ClaimValidationError, PermissionDeniedError) as exc:
        db.rollback()
        pol_query = select(Policy).where(Policy.status == PolicyStatus.ACTIVE)
        if user.role == UserRole.CUSTOMER:
            pol_query = pol_query.where(Policy.customer_id == user.customer_id)
        available_policies = db.scalars(pol_query).all()
        return templates.TemplateResponse(
            request=request,
            name="claim_register.html",
            context={
                "request": request,
                "user": user,
                "csrf_token": csrf_token,
                "available_policies": available_policies,
                "active_tab": "new_claim",
                "alert_message": str(exc),
                "alert_type": "error",
            },
            status_code=400,
        )


@router.get("/claims/{claim_id}", response_class=HTMLResponse)
def claim_detail(
    claim_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    msg: str | None = None,
    msg_type: str = "info",
) -> Response:
    """Render claim details with role-based data redaction."""
    user, session_id, csrf_token = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)

    claim = db.get(Claim, claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")

    # Object-level access verification
    check_claim_access(user, claim, action="READ")

    policy = db.get(Policy, claim.policy_id)
    customer = db.get(Customer, claim.customer_id)
    docs = db.scalars(select(ClaimDocument).where(ClaimDocument.claim_id == claim.id)).all()

    # Coverage info
    coverage = db.scalars(
        select(Coverage).where(
            Coverage.policy_id == claim.policy_id,
            Coverage.claim_category == claim.claim_category,
        )
    ).first()

    display_status = (
        get_customer_safe_status(claim.status)
        if user.role == UserRole.CUSTOMER
        else claim.status.value
    )

    claim_view = {
        "id": str(claim.id),
        "claim_ref": claim.claim_ref,
        "status": claim.status.value,
        "display_status": display_status,
        "registered_on": claim.registered_on.isoformat(),
        "policy_no": policy.policy_no if policy else "N/A",
        "product_type": policy.product_type.value if policy else "N/A",
        "claim_category": claim.claim_category,
        "incident_date": claim.incident_date.isoformat(),
        "requested_amount": claim.requested_amount,
        "incident_location": claim.incident_location,
        "incident_description": claim.incident_description,
        "customer_no": customer.customer_no if customer else "N/A",
        "policy_status": policy.status.value if policy else "N/A",
        "sum_insured": policy.sum_insured if policy else Decimal("0.00"),
        "policy_start": policy.start_date.isoformat() if policy else "-",
        "policy_end": policy.end_date.isoformat() if policy else "-",
        "coverage_remaining": (
            (coverage.limit_amount - coverage.consumed_amount) if coverage else Decimal("0.00")
        ),
        "legal_hold": getattr(claim, "legal_hold", False),
        "documents": [
            {
                "id": str(d.id),
                "doc_code": d.doc_code,
                "file_name": d.file_name,
                "status": d.status.value,
                "created_at": d.created_at.strftime("%Y-%m-%d %H:%M"),
            }
            for d in docs
        ],
    }

    # Role-based risk data visibility (Decision D6)
    risk_data: dict[str, Any] | None = None
    is_inv_mgr = user.role in (UserRole.INVESTIGATOR, UserRole.MANAGER)
    is_assessor = user.role == UserRole.ASSESSOR

    if is_inv_mgr or is_assessor:
        risk_service = RiskService(db=db)
        eval_record = risk_service.get_latest_risk_evaluation(claim_id=claim.id, actor=user)
        if eval_record:
            cat_val = (
                eval_record.category.value
                if hasattr(eval_record.category, "value")
                else str(eval_record.category)
            )
            comp_val = (
                eval_record.completeness.value
                if hasattr(eval_record.completeness, "value")
                else str(eval_record.completeness)
            )
            if is_inv_mgr:
                indicators = []
                for ind in eval_record.indicator_results:
                    indicators.append(
                        {
                            "rule_id": ind.indicator_id,
                            "name": ind.indicator_id,
                            "weight": ind.weight,
                            "triggered": ind.triggered,
                            "skipped": ind.status.value == "SKIPPED"
                            if hasattr(ind.status, "value")
                            else str(ind.status) == "SKIPPED",
                            "reason": ind.message,
                        }
                    )
                risk_data = {
                    "score": eval_record.score,
                    "score_category": cat_val,
                    "completeness_state": comp_val,
                    "indicators": indicators,
                }
            elif is_assessor:
                # Assessor: Category and completeness only; raw indicators stripped
                risk_data = {
                    "score_category": cat_val,
                    "completeness_state": comp_val,
                }

    return templates.TemplateResponse(
        request=request,
        name="claim_detail.html",
        context={
            "request": request,
            "user": user,
            "csrf_token": csrf_token,
            "claim": claim_view,
            "risk_data": risk_data,
            "is_investigator_or_manager": is_inv_mgr,
            "is_assessor": is_assessor,
            "active_tab": "claims",
            "alert_message": msg,
            "alert_type": msg_type,
        },
    )


# ==============================================================================
# Document Management & Review
# ==============================================================================


@router.get("/claims/{claim_id}/documents", response_class=HTMLResponse)
def documents_page(
    claim_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    msg: str | None = None,
    msg_type: str = "info",
) -> Response:
    """Render documents view for uploading and reviewing."""
    user, session_id, csrf_token = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)

    claim = db.get(Claim, claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")

    check_claim_access(user, claim, action="READ")
    docs = db.scalars(
        select(ClaimDocument)
        .where(ClaimDocument.claim_id == claim.id)
        .order_by(ClaimDocument.created_at.desc())
    ).all()

    can_upload = claim.status not in (
        ClaimStatus.APPROVED,
        ClaimStatus.REJECTED,
        ClaimStatus.WITHDRAWN,
    )
    can_review = user.role in (UserRole.ASSESSOR, UserRole.MANAGER)

    return templates.TemplateResponse(
        request=request,
        name="documents.html",
        context={
            "request": request,
            "user": user,
            "csrf_token": csrf_token,
            "claim": claim,
            "documents": docs,
            "can_upload": can_upload,
            "can_review": can_review,
            "active_tab": "claims",
            "alert_message": msg,
            "alert_type": msg_type,
        },
    )


@router.post("/claims/{claim_id}/documents/upload")
def document_upload_action(
    claim_id: uuid.UUID,
    request: Request,
    doc_code: str = Form(...),
    document_date: str = Form(default=""),
    named_person: str = Form(default=""),
    csrf_token: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Response:
    """Upload verification document."""
    user, session_id, _ = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if not session_id or not validate_csrf_token(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    from datetime import date as dt_date

    doc_date_val = dt_date.fromisoformat(document_date) if document_date.strip() else None
    content = file.file.read()

    try:
        service = ClaimService(db=db)
        service.upload_document(
            claim_id=claim_id,
            doc_code=doc_code.strip(),
            file_name=file.filename or "upload.pdf",
            content=content,
            mime_type=file.content_type or "application/pdf",
            actor=user,
            document_date=doc_date_val,
            named_person=named_person.strip() or None,
        )
        db.commit()
        return RedirectResponse(
            url=f"/ui/claims/{claim_id}/documents?msg=Document+uploaded+successfully&msg_type=success",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/ui/claims/{claim_id}/documents?msg={exc}&msg_type=error",
            status_code=303,
        )


@router.post("/claims/{claim_id}/documents/{doc_id}/review")
def document_review_action(
    claim_id: uuid.UUID,
    doc_id: uuid.UUID,
    request: Request,
    review_status: str = Form(...),
    remarks: str = Form(default=""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Review document validity."""
    user, session_id, _ = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if not session_id or not validate_csrf_token(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    try:
        service = ClaimService(db=db)
        req = DocumentReviewRequest(
            status=DocumentStatus(review_status.upper()),
            review_remarks=remarks.strip() or None,
            reason_code="DOCUMENT_INVALID" if review_status.upper() == "INVALID" else None,
        )
        service.review_document(
            claim_id=claim_id,
            document_id=doc_id,
            review_req=req,
            actor=user,
        )
        db.commit()
        return RedirectResponse(
            url=f"/ui/claims/{claim_id}/documents?msg=Document+review+recorded&msg_type=success",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/ui/claims/{claim_id}/documents?msg={exc}&msg_type=error",
            status_code=303,
        )


# ==============================================================================
# Assessment Worksheet View
# ==============================================================================


@router.get("/claims/{claim_id}/assessment", response_class=HTMLResponse)
def assessment_page(
    claim_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    msg: str | None = None,
    msg_type: str = "info",
) -> Response:
    """Render assessment worksheet."""
    user, session_id, csrf_token = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if user.role not in (UserRole.ASSESSOR, UserRole.MANAGER):
        raise HTTPException(
            status_code=403, detail="Assessment restricted to assessors and managers"
        )

    claim = db.get(Claim, claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")

    coverage = db.scalars(
        select(Coverage).where(
            Coverage.policy_id == claim.policy_id,
            Coverage.claim_category == claim.claim_category,
        )
    ).first()

    existing_assessment = db.scalars(
        select(Assessment)
        .where(Assessment.claim_id == claim.id)
        .order_by(Assessment.created_at.desc())
    ).first()

    can_submit = claim.status in (ClaimStatus.READY_FOR_ASSESSMENT, ClaimStatus.ASSESSED)

    return templates.TemplateResponse(
        request=request,
        name="assessment.html",
        context={
            "request": request,
            "user": user,
            "csrf_token": csrf_token,
            "claim": claim,
            "coverage": coverage,
            "existing_assessment": existing_assessment,
            "can_submit_assessment": can_submit,
            "active_tab": "claims",
            "alert_message": msg,
            "alert_type": msg_type,
        },
    )


@router.post("/claims/{claim_id}/assessment")
def assessment_submit_action(
    claim_id: uuid.UUID,
    request: Request,
    assessed_loss: Decimal = Form(...),
    deductible: Decimal = Form(...),
    remarks: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Record loss assessment."""
    user, session_id, _ = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if not session_id or not validate_csrf_token(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    try:
        claim = db.get(Claim, claim_id)
        if not claim:
            raise HTTPException(status_code=404, detail="Claim not found")

        coverage = db.scalars(
            select(Coverage).where(
                Coverage.policy_id == claim.policy_id,
                Coverage.claim_category == claim.claim_category,
            )
        ).first()

        policy = claim.policy
        remaining_limit = (
            coverage.limit_amount
            if coverage and coverage.limit_scope == LimitScope.PER_CLAIM
            else max(Decimal("0.00"), coverage.limit_amount - coverage.consumed_amount)
            if coverage
            else Decimal("0.00")
        )
        copay_percent = coverage.copay_percent if coverage else Decimal("0.00")

        engine_input = AssessmentInput(
            product_type=policy.product_type.value,
            requested_amount=claim.requested_amount,
            remaining_limit=remaining_limit,
            assessed_loss=assessed_loss,
            deductible=deductible,
            copay_percent=copay_percent,
            depreciation=Decimal("0.00"),
            sum_insured=policy.sum_insured,
            value_at_risk=Decimal("0.00"),
            components=(),
        )
        cap_output = calculate_assessment_cap(engine_input)

        service = AssessmentService(db=db)
        payload = AssessmentCreate(
            assessed_loss=assessed_loss,
            eligible_amount=cap_output.max_eligible_amount,
            deductible=deductible,
            remarks=remarks.strip(),
        )
        service.create_assessment(claim_id=claim_id, payload=payload, actor=user)
        db.commit()
        return RedirectResponse(
            url=f"/ui/claims/{claim_id}?msg=Assessment+calculated+and+saved&msg_type=success",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/ui/claims/{claim_id}/assessment?msg={exc}&msg_type=error",
            status_code=303,
        )


# ==============================================================================
# Investigation Console View
# ==============================================================================


@router.get("/claims/{claim_id}/investigation", response_class=HTMLResponse)
def investigation_page(
    claim_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    msg: str | None = None,
    msg_type: str = "info",
) -> Response:
    """Render investigation console."""
    user, session_id, csrf_token = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if user.role not in (UserRole.INVESTIGATOR, UserRole.MANAGER):
        raise HTTPException(
            status_code=403, detail="Investigation restricted to investigators and managers"
        )

    claim = db.get(Claim, claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")

    risk_service = RiskService(db=db)
    eval_rec = risk_service.get_latest_risk_evaluation(claim_id=claim.id, actor=user)
    indicators = []
    if eval_rec:
        for ind in eval_rec.indicator_results:
            indicators.append(
                {
                    "rule_id": ind.indicator_id,
                    "name": ind.indicator_id,
                    "weight": ind.weight,
                    "triggered": ind.triggered,
                    "skipped": ind.status.value == "SKIPPED"
                    if hasattr(ind.status, "value")
                    else str(ind.status) == "SKIPPED",
                    "reason": ind.message,
                }
            )
    risk_data = {
        "score": eval_rec.score if eval_rec else None,
        "indicators": indicators,
    }

    can_submit = claim.status in (ClaimStatus.UNDER_INVESTIGATION, ClaimStatus.ESCALATED)

    return templates.TemplateResponse(
        request=request,
        name="investigation.html",
        context={
            "request": request,
            "user": user,
            "csrf_token": csrf_token,
            "claim": claim,
            "risk_data": risk_data,
            "can_submit_findings": can_submit,
            "active_tab": "claims",
            "alert_message": msg,
            "alert_type": msg_type,
        },
    )


@router.post("/claims/{claim_id}/investigation")
def investigation_submit_action(
    claim_id: uuid.UUID,
    request: Request,
    outcome: str = Form(...),
    findings_summary: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Submit investigation determination."""
    user, session_id, _ = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if not session_id or not validate_csrf_token(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    try:
        service = InvestigationService(db=db)
        out_enum = InvestigationOutcome(outcome.upper())
        service.add_finding(claim_id=claim_id, finding_text=findings_summary.strip(), actor=user)
        service.complete_investigation(
            claim_id=claim_id,
            outcome=out_enum,
            actor=user,
            remarks=findings_summary.strip(),
        )
        db.commit()
        return RedirectResponse(
            url=f"/ui/claims/{claim_id}?msg=Investigation+findings+submitted&msg_type=success",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/ui/claims/{claim_id}/investigation?msg={exc}&msg_type=error",
            status_code=303,
        )


# ==============================================================================
# Human Decision Panel View
# ==============================================================================


@router.get("/claims/{claim_id}/decision", response_class=HTMLResponse)
def decision_page(
    claim_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    msg: str | None = None,
    msg_type: str = "info",
) -> Response:
    """Render human decision panel."""
    user, session_id, csrf_token = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if user.role not in (UserRole.STAFF, UserRole.MANAGER):
        raise HTTPException(
            status_code=403, detail="Decision panel restricted to staff and managers"
        )

    claim = db.get(Claim, claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")

    assessment = db.scalars(
        select(Assessment)
        .where(Assessment.claim_id == claim.id)
        .order_by(Assessment.created_at.desc())
    ).first()

    existing_decision = db.scalars(
        select(Decision).where(Decision.claim_id == claim.id).order_by(Decision.created_at.desc())
    ).first()

    can_make = claim.status == ClaimStatus.ASSESSED

    return templates.TemplateResponse(
        request=request,
        name="decision.html",
        context={
            "request": request,
            "user": user,
            "csrf_token": csrf_token,
            "claim": claim,
            "assessment": assessment,
            "existing_decision": existing_decision,
            "can_make_decision": can_make,
            "active_tab": "claims",
            "alert_message": msg,
            "alert_type": msg_type,
        },
    )


@router.post("/claims/{claim_id}/decision")
def decision_submit_action(
    claim_id: uuid.UUID,
    request: Request,
    outcome: str = Form(...),
    approved_amount: Decimal = Form(default=Decimal("0.00")),
    reason_code: str = Form(default=""),
    remarks: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Execute human claim determination."""
    user, session_id, _ = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if not session_id or not validate_csrf_token(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    try:
        service = DecisionService(db=db)
        dec_type = DecisionType(outcome.upper())
        reasons = (
            [reason_code.strip()]
            if (dec_type == DecisionType.REJECTED and reason_code.strip())
            else None
        )
        payload = DecisionCreate(
            decision=dec_type,
            approved_amount=approved_amount if dec_type == DecisionType.APPROVED else None,
            reason_codes=reasons,
            remarks=remarks.strip(),
        )
        service.make_decision(claim_id=claim_id, payload=payload, actor=user)
        db.commit()
        return RedirectResponse(
            url=f"/ui/claims/{claim_id}?msg=Claim+decision+executed&msg_type=success",
            status_code=303,
        )
    except (DecisionPermissionError, ValueError, Exception) as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/ui/claims/{claim_id}/decision?msg={exc}&msg_type=error",
            status_code=303,
        )


# ==============================================================================
# Operational Analytics Dashboard
# ==============================================================================


@router.get("/analytics", response_class=HTMLResponse)
def analytics_dashboard(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Render operational analytics dashboard with zero risk data leakage."""
    user, session_id, csrf_token = get_ui_session(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=303)
    if user.role not in (UserRole.STAFF, UserRole.MANAGER, UserRole.ADMIN):
        raise HTTPException(
            status_code=403, detail="Analytics access restricted to staff and managers"
        )

    total_claims = db.scalar(select(func.count(Claim.id))) or 0
    status_counts = db.execute(
        select(Claim.status, func.count(Claim.id)).group_by(Claim.status)
    ).all()
    claims_by_status = {
        s[0].value if hasattr(s[0], "value") else str(s[0]): s[1] for s in status_counts
    }

    total_approved = db.scalar(
        select(func.sum(Decision.approved_amount)).where(Decision.decision == DecisionType.APPROVED)
    ) or Decimal("0.00")

    approved_count = (
        db.scalar(select(func.count(Decision.id)).where(Decision.decision == DecisionType.APPROVED))
        or 0
    )
    avg_approved = (total_approved / approved_count) if approved_count > 0 else Decimal("0.00")

    analytics_data = {
        "total_claims": total_claims,
        "claims_by_status": claims_by_status,
        "total_approved_amount": total_approved,
        "average_approval_amount": avg_approved,
    }

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "request": request,
            "user": user,
            "csrf_token": csrf_token,
            "analytics": analytics_data,
            "active_tab": "analytics",
        },
    )

"""Claim service managing registration, document lifecycle, and state machine transitions."""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.schema import hash_free_text
from app.audit.writer import write_audit_log
from app.db.base import utc_now
from app.db.lock_utils import set_lock_timeout
from app.db.models import (
    Claim,
    ClaimDocument,
    ClaimStatusHistory,
    Coverage,
    Policy,
    PolicyInsuredItem,
    User,
)
from app.domain.constants import CANONICAL_SENSITIVE_DOC_CODES
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    DocumentStatus,
    PolicyStatus,
    UserRole,
)
from app.domain.state_machine import validate_transition
from app.schemas.claims import ClaimCreate, ClaimStatusTransitionRequest
from app.schemas.documents import DocumentReviewRequest
from app.security.access_log import log_access
from app.security.rbac import (
    PermissionDeniedError,
    check_claim_access,
    check_document_access,
    check_separation_of_duties,
)
from app.services.document_storage import DocumentStorage, default_storage

SENSITIVE_DOC_CODES = CANONICAL_SENSITIVE_DOC_CODES


class StaleStatusVersionError(Exception):
    """Raised when status_version token does not match current database state."""

    pass


class ClaimValidationError(ValueError):
    """Raised when claim registration fails business validation rules."""

    pass


class ClaimService:
    """Core domain service for claims and documents."""

    def __init__(self, db: Session, storage: DocumentStorage | None = None) -> None:
        self.db = db
        self.storage = storage or default_storage

    def generate_claim_ref(self, incident_year: int) -> str:
        """Generate unique sequential claim reference formatted as CLM-YYYY-NNNNNN."""
        prefix = f"CLM-{incident_year}-"
        stmt = (
            select(Claim.claim_ref)
            .where(Claim.claim_ref.like(f"{prefix}%"))
            .order_by(Claim.claim_ref.desc())
            .limit(1)
        )
        bind = self.db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()

        latest_ref = self.db.scalar(stmt)
        if latest_ref:
            try:
                seq = int(latest_ref.split("-")[-1]) + 1
            except ValueError:
                seq = 1
        else:
            seq = 1
        return f"{prefix}{seq:06d}"

    def register_claim(self, payload: ClaimCreate, actor: User) -> Claim:
        """Register a new claim on an active policy with audit logging and reference generation."""
        # Check idempotency: if source_system and external_ref provided, check for existing
        if payload.source_system and payload.external_ref:
            existing = self.db.scalars(
                select(Claim).where(
                    Claim.source_system == payload.source_system,
                    Claim.external_ref == payload.external_ref,
                )
            ).first()
            if existing:
                return existing

        # Fetch and validate policy
        policy = self.db.get(Policy, payload.policy_id)
        if not policy:
            raise ClaimValidationError(f"Policy '{payload.policy_id}' not found")

        if policy.status != PolicyStatus.ACTIVE:
            raise ClaimValidationError(
                f"Policy '{policy.policy_no}' is {policy.status.value}, but only ACTIVE policies can receive claims"
            )

        # Validate incident date is within policy coverage period
        if not (policy.start_date <= payload.incident_date <= policy.end_date):
            raise ClaimValidationError(
                f"Incident date {payload.incident_date} falls outside policy period ({policy.start_date} to {policy.end_date})"
            )

        # Enforce claimant ownership and customer role boundaries
        target_customer_id = policy.customer_id
        if actor.role == UserRole.CUSTOMER:
            if actor.customer_id != policy.customer_id:
                raise PermissionDeniedError(
                    "Customers can only register claims against their own active policies"
                )
            channel = ClaimChannel.CUSTOMER
        elif actor.role == UserRole.STAFF:
            if payload.customer_id and payload.customer_id != policy.customer_id:
                raise ClaimValidationError(
                    "Specified customer_id does not match the policy holder customer_id"
                )
            channel = ClaimChannel.STAFF
        elif actor.role == UserRole.ADMIN:
            channel = ClaimChannel.STAFF
        else:
            raise PermissionDeniedError(f"Role '{actor.role.value}' cannot register claims")

        # Validate coverage existence and exclusion
        coverage = self.db.scalars(
            select(Coverage).where(
                Coverage.policy_id == policy.id,
                Coverage.claim_category == payload.claim_category,
            )
        ).first()
        if not coverage:
            raise ClaimValidationError(
                f"Claim category '{payload.claim_category}' has no coverage under policy '{policy.policy_no}'"
            )
        if coverage.is_excluded:
            raise ClaimValidationError(
                f"Claim category '{payload.claim_category}' is explicitly excluded on policy '{policy.policy_no}'"
            )

        # Validate insured item if provided (Section E.61)
        if payload.insured_item_id:
            item = self.db.get(PolicyInsuredItem, payload.insured_item_id)
            if not item or item.policy_id != policy.id:
                raise ClaimValidationError(
                    f"Insured item '{payload.insured_item_id}' does not belong to policy '{policy.policy_no}'"
                )

        # Generate unique sequential claim reference
        year = payload.incident_date.year
        claim_ref = self.generate_claim_ref(year)

        claim_id = uuid.uuid4()
        claim = Claim(
            id=claim_id,
            claim_ref=claim_ref,
            policy_id=policy.id,
            customer_id=target_customer_id,
            insured_item_id=payload.insured_item_id,
            claim_category=payload.claim_category,
            incident_date=payload.incident_date,
            incident_location=payload.incident_location,
            incident_description=payload.incident_description,
            requested_amount=payload.requested_amount,
            registered_at=utc_now(),
            registered_by=actor.id,
            channel=channel,
            status=ClaimStatus.REGISTERED,
            status_version=1,
            source_system=payload.source_system,
            external_ref=payload.external_ref,
        )
        self.db.add(claim)

        # Record initial status history
        history = ClaimStatusHistory(
            claim_id=claim_id,
            from_status=ClaimStatus.DRAFT.value,
            to_status=ClaimStatus.REGISTERED.value,
            changed_by=actor.id,
            reason="Initial claim registration",
            created_at=utc_now(),
        )
        self.db.add(history)

        # Write genesis audit log for this independent claim chain
        write_audit_log(
            db=self.db,
            action="CLAIM_REGISTERED",
            entity_type="Claim",
            entity_id=str(claim_id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim_id,
            event_data={
                "claim_ref": claim_ref,
                "policy_id": str(policy.id),
                "customer_id": str(target_customer_id),
                "claim_category": payload.claim_category,
                "requested_amount": payload.requested_amount,
                "channel": channel.value,
            },
        )
        self.db.flush()
        return claim

    def upload_document(
        self,
        claim_id: uuid.UUID,
        doc_code: str,
        file_name: str,
        content: bytes,
        mime_type: str,
        actor: User,
        document_date: date | None = None,
        named_person: str | None = None,
    ) -> ClaimDocument:
        """Upload and attach a document with superseding and audit logging."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise ClaimValidationError(f"Claim '{claim_id}' not found")

        # Object-level check: customers can only upload to own claims; staff to registered/assigned
        check_claim_access(actor, claim, action="MUTATE")

        # Section B.16: Superseding. Replacing prior VALID/PENDING_REVIEW marks prior row SUPERSEDED
        existing_docs = self.db.scalars(
            select(ClaimDocument).where(
                ClaimDocument.claim_id == claim_id,
                ClaimDocument.doc_code == doc_code,
                ClaimDocument.status.in_([DocumentStatus.PENDING_REVIEW, DocumentStatus.VALID]),
            )
        ).all()
        for doc in existing_docs:
            doc.status = DocumentStatus.SUPERSEDED

        # Store binary securely via storage adapter
        storage_key, sha256_hash, size_bytes = self.storage.store(
            file_name=file_name,
            content=content,
            mime_type=mime_type,
        )

        new_doc = ClaimDocument(
            id=uuid.uuid4(),
            claim_id=claim_id,
            doc_code=doc_code,
            file_name=file_name,
            storage_key=storage_key,
            sha256=sha256_hash,
            size_bytes=size_bytes,
            mime_type=mime_type,
            document_date=document_date,
            named_person=named_person,
            status=DocumentStatus.PENDING_REVIEW,
            uploaded_by=actor.id,
            created_at=utc_now(),
        )
        self.db.add(new_doc)

        # Write audit log for document upload
        write_audit_log(
            db=self.db,
            action="DOCUMENT_UPLOADED",
            entity_type="ClaimDocument",
            entity_id=str(new_doc.id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim_id,
            event_data={
                "doc_code": doc_code,
                "file_name_sha256": hash_free_text(file_name),
                "sha256": sha256_hash,
                "size_bytes": size_bytes,
                "mime_type": mime_type,
            },
        )
        self.db.flush()
        return new_doc

    def review_document(
        self,
        claim_id: uuid.UUID,
        document_id: uuid.UUID,
        review_req: DocumentReviewRequest,
        actor: User,
    ) -> ClaimDocument:
        """Review a claim document (Assessor / Manager per D8)."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise ClaimValidationError(f"Claim '{claim_id}' not found")

        # Object-level check
        check_claim_access(actor, claim, action="MUTATE")

        # Role constraint: Assessor or Manager only (D8: not staff, not admin)
        if actor.role not in (UserRole.ASSESSOR, UserRole.MANAGER):
            raise PermissionDeniedError(
                f"Role '{actor.role.value}' does not have document.review permission (D8)"
            )

        doc = self.db.scalars(
            select(ClaimDocument).where(
                ClaimDocument.claim_id == claim_id,
                ClaimDocument.id == document_id,
            )
        ).first()
        if not doc:
            raise ClaimValidationError(f"Document '{document_id}' not found on claim '{claim_id}'")

        # Separation of duties: Reviewer cannot be the uploader (P1 task 8, D8)
        check_separation_of_duties(actor, claim, action="REVIEW_DOCUMENT", document=doc)

        # Enforce mandatory remarks and reason code on INVALID
        doc.status = review_req.status
        doc.reason_code = review_req.reason_code
        doc.review_remarks = review_req.review_remarks
        doc.reviewed_by = actor.id
        doc.reviewed_at = utc_now()

        # Audit write
        write_audit_log(
            db=self.db,
            action="DOCUMENT_REVIEWED",
            entity_type="ClaimDocument",
            entity_id=str(doc.id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim_id,
            event_data={
                "doc_code": doc.doc_code,
                "status": doc.status.value,
                "reason_code": doc.reason_code,
                "remarks_sha256": hash_free_text(doc.review_remarks),
            },
        )
        self.db.flush()
        return doc

    def get_document_for_download(
        self, claim_id: uuid.UUID, document_id: uuid.UUID, actor: User
    ) -> tuple[ClaimDocument, bytes]:
        """Verify access, retrieve document binary, and log sensitive document views."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise ClaimValidationError(f"Claim '{claim_id}' not found")

        doc = self.db.scalars(
            select(ClaimDocument).where(
                ClaimDocument.claim_id == claim_id,
                ClaimDocument.id == document_id,
            )
        ).first()
        if not doc:
            raise ClaimValidationError(f"Document '{document_id}' not found")

        # Check document-level access, including sensitive identity/medical docs
        check_document_access(actor, claim, doc)

        # Fail-closed access_log commit before streaming binary (P1 task 5)
        is_sensitive = doc.doc_code in SENSITIVE_DOC_CODES
        if is_sensitive or (claim.customer_id != actor.customer_id):
            log_access(
                db=self.db,
                actor=actor,
                claim_id=claim.id,
                document_id=doc.id,
                resource_type="DOCUMENT",
                action="DOWNLOAD",
            )

        # Section H.88: Log every sensitive-document view/download
        if doc.doc_code in SENSITIVE_DOC_CODES:
            write_audit_log(
                db=self.db,
                action="SENSITIVE_DOCUMENT_DOWNLOADED",
                entity_type="ClaimDocument",
                entity_id=str(doc.id),
                actor_role=actor.role.value,
                actor_id=actor.id,
                claim_id=claim_id,
                event_data={
                    "doc_code": doc.doc_code,
                    "file_name_sha256": hash_free_text(doc.file_name),
                    "sha256": doc.sha256,
                },
            )

        content = self.storage.retrieve(doc.storage_key)
        return doc, content

    def transition_status(
        self,
        claim_id: uuid.UUID,
        req: ClaimStatusTransitionRequest,
        actor: User,
    ) -> Claim:
        """Execute a state machine transition with optimistic locking and row-level locking."""
        set_lock_timeout(self.db)
        stmt = select(Claim).where(Claim.id == claim_id)
        bind = self.db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()

        claim = self.db.scalars(stmt).first()
        if not claim:
            raise ClaimValidationError(f"Claim '{claim_id}' not found")

        # Object-level check: actor must have mutation permission on this claim
        check_claim_access(actor, claim, action="MUTATE")

        if actor.role == UserRole.CUSTOMER:
            raise PermissionDeniedError("Customers cannot directly transition claim status")

        # Verify optimistic locking token
        if claim.status_version != req.expected_status_version:
            raise StaleStatusVersionError(
                f"Stale claim state: expected status_version {req.expected_status_version}, current is {claim.status_version}"
            )

        # Validate state machine transition legality
        validate_transition(claim.status, req.to_status)

        # Section G.77: Rejecting from DOCUMENTS_PENDING requires mandatory remarks and reason_code
        if req.to_status == ClaimStatus.REJECTED:
            if not req.remarks or len(req.remarks.strip()) < 5:
                raise ClaimValidationError("Remarks are mandatory when rejecting a claim")
            if not req.reason_code or not req.reason_code.strip():
                raise ClaimValidationError("reason_code is mandatory when rejecting a claim")

        old_status = claim.status
        claim.status = req.to_status
        claim.status_version += 1

        # Record history
        history = ClaimStatusHistory(
            claim_id=claim_id,
            from_status=old_status.value,
            to_status=req.to_status.value,
            changed_by=actor.id,
            reason=req.remarks or f"Transition to {req.to_status.value}",
            created_at=utc_now(),
        )
        self.db.add(history)

        # Audit write in same transaction
        write_audit_log(
            db=self.db,
            action="CLAIM_STATUS_TRANSITION",
            entity_type="Claim",
            entity_id=str(claim_id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim_id,
            event_data={
                "from_status": old_status.value,
                "to_status": req.to_status.value,
                "status_version": claim.status_version,
                "reason_code": req.reason_code,
                "remarks_sha256": hash_free_text(req.remarks),
            },
        )
        self.db.flush()
        return claim

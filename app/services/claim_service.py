"""Claim service managing registration, document lifecycle, and state machine transitions."""

import uuid
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.schema import hash_free_text
from app.audit.writer import write_audit_log
from app.config import settings
from app.db.base import utc_now
from app.db.lock_utils import set_lock_timeout
from app.db.models import (
    Claim,
    ClaimDocument,
    ClaimStatusHistory,
    Coverage,
    Customer,
    DocumentIdentityAttestation,
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
    ProductType,
    UserRole,
)
from app.domain.state_machine import validate_transition
from app.schemas.claims import ClaimCreate, ClaimStatusTransitionRequest
from app.schemas.documents import DocumentReviewRequest, IdentityAttestationCreate
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

        # Task 7: Insured item and member verification
        if payload.insured_item_id:
            item = self.db.get(PolicyInsuredItem, payload.insured_item_id)
            if not item or item.policy_id != policy.id:
                raise ClaimValidationError(
                    f"Insured item '{payload.insured_item_id}' does not belong to policy '{policy.policy_no}'"
                )
            if policy.product_type == ProductType.VEHICLE and item.item_type.upper() != "VEHICLE":
                raise ClaimValidationError(
                    f"Claimed item '{payload.insured_item_id}' has type '{item.item_type}', but VEHICLE is required"
                )
            if policy.product_type == ProductType.PROPERTY and item.item_type.upper() != "PROPERTY":
                raise ClaimValidationError(
                    f"Claimed item '{payload.insured_item_id}' has type '{item.item_type}', but PROPERTY is required"
                )
            if policy.product_type == ProductType.HEALTH and item.item_type.upper() != "MEMBER":
                raise ClaimValidationError(
                    f"Claimed item '{payload.insured_item_id}' has type '{item.item_type}', but MEMBER is required"
                )
        else:
            # If policy has declared insured vehicle/property items, require one to be claimed
            if policy.product_type == ProductType.VEHICLE:
                has_vehicles = any(i.item_type.upper() == "VEHICLE" for i in policy.insured_items)
                if has_vehicles:
                    raise ClaimValidationError(
                        "Vehicle policy requires specifying an insured vehicle item"
                    )
            elif policy.product_type == ProductType.PROPERTY:
                has_properties = any(
                    i.item_type.upper() == "PROPERTY" for i in policy.insured_items
                )
                if has_properties:
                    raise ClaimValidationError(
                        "Property policy requires specifying an insured property item"
                    )

        # Generate unique sequential claim reference
        year = payload.incident_date.year
        claim_ref = self.generate_claim_ref(year)

        # Task 10: Store registered_at (UTC), registered_on (business date), and registered_tz
        now_utc = utc_now()
        tz_name = settings.BUSINESS_TIMEZONE
        tz = ZoneInfo(tz_name)
        registered_on = now_utc.astimezone(tz).date()

        claim_id = uuid.uuid4()
        claim = Claim(
            id=claim_id,
            claim_ref=claim_ref,
            policy_id=policy.id,
            customer_id=target_customer_id,
            insured_item_id=payload.insured_item_id,
            incident_group_id=payload.incident_group_id,
            claim_category=payload.claim_category,
            incident_date=payload.incident_date,
            incident_location=payload.incident_location,
            incident_description=payload.incident_description,
            requested_amount=payload.requested_amount,
            registered_at=now_utc,
            registered_on=registered_on,
            registered_tz=tz_name,
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
        """Upload and attach a document with superseding, audit logging, and transactional revalidation."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise ClaimValidationError(f"Claim '{claim_id}' not found")

        if claim.status in (ClaimStatus.APPROVED, ClaimStatus.REJECTED, ClaimStatus.WITHDRAWN):
            raise ClaimValidationError(
                f"Cannot upload documents for claim in terminal status '{claim.status.value}'"
            )

        # Object-level check: customers can only upload to own claims; staff to registered/assigned
        check_claim_access(actor, claim, action="MUTATE")

        # Section B.16: Superseding. Replacing prior row marks prior row SUPERSEDED
        existing_docs = self.db.scalars(
            select(ClaimDocument).where(
                ClaimDocument.claim_id == claim_id,
                ClaimDocument.doc_code == doc_code,
                ClaimDocument.status != DocumentStatus.SUPERSEDED,
            )
        ).all()
        for doc in existing_docs:
            doc.status = DocumentStatus.SUPERSEDED
            write_audit_log(
                db=self.db,
                action="DOCUMENT_SUPERSEDED",
                entity_type="ClaimDocument",
                entity_id=str(doc.id),
                actor_role=actor.role.value,
                actor_id=actor.id,
                claim_id=claim_id,
                event_data={
                    "doc_code": doc.doc_code,
                    "old_document_id": str(doc.id),
                    "document_sha256": doc.sha256,
                },
            )

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

        # Revalidation (fix F8): Upload and replacement trigger revalidation in same transaction
        if claim.status in (
            ClaimStatus.REGISTERED,
            ClaimStatus.DOCUMENTS_PENDING,
            ClaimStatus.VALIDATION_FAILED,
            ClaimStatus.INFO_REQUESTED,
        ):
            from app.services.validation_service import ValidationService

            ValidationService(self.db).validate_claim(claim_id=claim_id, actor=actor)

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

        if doc.status == DocumentStatus.SUPERSEDED:
            raise ClaimValidationError("Cannot review superseded document")

        if review_req.status == DocumentStatus.SUPERSEDED:
            raise ClaimValidationError("Cannot set document status to SUPERSEDED")

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

        # If attestation is attached to review request
        if review_req.attestation:
            self.attest_document_identity(
                claim_id=claim_id,
                document_id=document_id,
                payload=review_req.attestation,
                actor=actor,
            )

        # Revalidation (fix F8): Review triggers revalidation in the same transaction
        if claim.status in (
            ClaimStatus.REGISTERED,
            ClaimStatus.DOCUMENTS_PENDING,
            ClaimStatus.VALIDATION_FAILED,
            ClaimStatus.INFO_REQUESTED,
        ):
            from app.services.validation_service import ValidationService

            ValidationService(self.db).validate_claim(claim_id=claim_id, actor=actor)

        return doc

    def attest_document_identity(
        self,
        claim_id: uuid.UUID,
        document_id: uuid.UUID,
        payload: IdentityAttestationCreate,
        actor: User,
    ) -> DocumentIdentityAttestation:
        """Submit append-only identity attestation revision for an identity-bearing document (Decision D4)."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise ClaimValidationError(f"Claim '{claim_id}' not found")

        check_claim_access(actor, claim, action="MUTATE")

        if actor.role not in (UserRole.ASSESSOR, UserRole.MANAGER):
            raise PermissionDeniedError(
                f"Role '{actor.role.value}' does not have identity attestation permission (D8)"
            )

        # Lock document row
        stmt = select(ClaimDocument).where(
            ClaimDocument.claim_id == claim_id,
            ClaimDocument.id == document_id,
        )
        bind = self.db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()

        doc = self.db.scalars(stmt).first()
        if not doc:
            raise ClaimValidationError(f"Document '{document_id}' not found on claim '{claim_id}'")

        if doc.status == DocumentStatus.SUPERSEDED:
            raise ClaimValidationError("Cannot attest superseded document")

        # Separation of duties: Reviewer cannot be uploader
        check_separation_of_duties(actor, claim, action="REVIEW_DOCUMENT", document=doc)

        # Fetch current latest revision for document
        latest_att = self.db.scalars(
            select(DocumentIdentityAttestation)
            .where(DocumentIdentityAttestation.document_id == document_id)
            .order_by(DocumentIdentityAttestation.rev.desc())
        ).first()

        curr_rev = latest_att.rev if latest_att else 0
        if payload.expected_rev != curr_rev:
            raise StaleStatusVersionError(
                f"Attestation revision conflict: expected rev {payload.expected_rev}, but current rev is {curr_rev}"
            )

        # Get subject's current identity version
        customer = self.db.get(Customer, claim.customer_id)
        subject_id_version = customer.identity_version if customer else 1

        new_rev = curr_rev + 1
        prev_rev = curr_rev if curr_rev > 0 else None

        attestation = DocumentIdentityAttestation(
            document_id=document_id,
            rev=new_rev,
            prev_rev=prev_rev,
            document_sha256=doc.sha256,
            result=payload.result,
            reason_code=payload.reason_code,
            remarks=payload.remarks,
            reviewer_id=actor.id,
            reviewer_role=actor.role.value,
            subject_identity_version=subject_id_version,
            created_at=utc_now(),
        )
        self.db.add(attestation)

        # Write audit log for identity attestation
        write_audit_log(
            db=self.db,
            action="IDENTITY_ATTESTED",
            entity_type="DocumentIdentityAttestation",
            entity_id=f"{document_id}:{new_rev}",
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim_id,
            event_data={
                "document_id": str(document_id),
                "rev": new_rev,
                "prev_rev": prev_rev,
                "document_sha256": doc.sha256,
                "result": payload.result.value,
                "reason_code": payload.reason_code,
                "remarks_sha256": hash_free_text(payload.remarks),
                "reviewer_role": actor.role.value,
                "subject_identity_version": subject_id_version,
            },
        )
        self.db.flush()

        # Revalidation (fix F8): Attestation change triggers revalidation
        if claim.status in (
            ClaimStatus.REGISTERED,
            ClaimStatus.DOCUMENTS_PENDING,
            ClaimStatus.VALIDATION_FAILED,
            ClaimStatus.INFO_REQUESTED,
        ):
            from app.services.validation_service import ValidationService

            ValidationService(self.db).validate_claim(claim_id=claim_id, actor=actor)

        return attestation

    def get_document_attestations(
        self,
        claim_id: uuid.UUID,
        document_id: uuid.UUID,
        actor: User,
    ) -> list[DocumentIdentityAttestation]:
        """List append-only identity attestations for a document in revision order."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise ClaimValidationError(f"Claim '{claim_id}' not found")

        check_claim_access(actor, claim, action="READ")

        doc = self.db.scalars(
            select(ClaimDocument).where(
                ClaimDocument.claim_id == claim_id,
                ClaimDocument.id == document_id,
            )
        ).first()
        if not doc:
            raise ClaimValidationError(f"Document '{document_id}' not found on claim '{claim_id}'")

        return list(
            self.db.scalars(
                select(DocumentIdentityAttestation)
                .where(DocumentIdentityAttestation.document_id == document_id)
                .order_by(DocumentIdentityAttestation.rev.asc())
            ).all()
        )

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

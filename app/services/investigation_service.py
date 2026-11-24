"""Investigation service orchestrating assignments, findings, escalations, and outcomes."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.audit.schema import hash_free_text
from app.audit.writer import write_audit_log
from app.db.base import utc_now
from app.db.models import Claim, ClaimStatusHistory, Investigation, InvestigationFinding, User
from app.domain.enums import ClaimStatus, InvestigationOutcome, InvestigationStatus, UserRole
from app.domain.state_machine import validate_transition
from app.security.rbac import check_claim_access


class InvestigationServiceError(Exception):
    """Base error for investigation operations."""

    pass


class InvestigationValidationError(InvestigationServiceError):
    """Raised when an investigation transition or input is invalid."""

    pass


class InvestigationPermissionError(InvestigationServiceError):
    """Raised when an actor lacks the necessary role or ownership."""

    pass


class InvestigationService:
    """Service managing investigations, findings, escalations, and conclusions."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_active_investigation(
        self, claim_id: uuid.UUID, actor: User | None = None
    ) -> Investigation:
        """Fetch the currently active (non-completed) investigation for a claim."""
        if actor is not None:
            claim = self.db.get(Claim, claim_id)
            if not claim:
                raise InvestigationServiceError(f"Claim '{claim_id}' not found")
            check_claim_access(actor, claim, action="READ")

        stmt = (
            select(Investigation)
            .where(
                Investigation.claim_id == claim_id,
                Investigation.status != InvestigationStatus.COMPLETED,
            )
            .options(selectinload(Investigation.findings))
            .order_by(Investigation.created_at.desc())
        )
        inv = self.db.scalars(stmt).first()
        if not inv:
            raise InvestigationServiceError(f"No active investigation found for claim '{claim_id}'")
        return inv

    def assign_investigator(
        self,
        claim_id: uuid.UUID,
        investigator_id: uuid.UUID,
        actor: User,
    ) -> Investigation:
        """Assign an investigator to an active claim investigation (Manager or Admin only)."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise InvestigationServiceError(f"Claim '{claim_id}' not found")
        check_claim_access(actor, claim, action="MUTATE")

        if actor.role not in (UserRole.MANAGER, UserRole.ADMIN):
            raise InvestigationPermissionError(
                f"Role '{actor.role.value}' is not authorized to assign investigators"
            )

        investigator = self.db.get(User, investigator_id)
        if not investigator:
            raise InvestigationValidationError(f"User '{investigator_id}' not found")
        if investigator.role != UserRole.INVESTIGATOR and investigator.role != UserRole.ADMIN:
            raise InvestigationValidationError(
                f"User '{investigator_id}' does not have the 'investigator' role"
            )

        investigation = self.get_active_investigation(claim_id, actor=actor)
        investigation.assigned_to = investigator_id
        investigation.assigned_by = actor.id

        write_audit_log(
            db=self.db,
            action="INVESTIGATION_ASSIGNED",
            entity_type="Investigation",
            entity_id=str(investigation.id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim_id,
            event_data={"assigned_to": str(investigator_id)},
        )

        self.db.commit()
        self.db.refresh(investigation)
        return investigation

    def start_review(self, claim_id: uuid.UUID, actor: User) -> Investigation:
        """Transition investigation from ASSIGNED to UNDER_REVIEW."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise InvestigationServiceError(f"Claim '{claim_id}' not found")
        check_claim_access(actor, claim, action="MUTATE")

        investigation = self.get_active_investigation(claim_id)

        # Actor must be assigned investigator, manager, or admin
        if actor.role not in (UserRole.MANAGER, UserRole.ADMIN):
            if investigation.assigned_to != actor.id:
                raise InvestigationPermissionError(
                    "Only the assigned investigator, manager, or admin can initiate review"
                )

        if investigation.status == InvestigationStatus.ASSIGNED:
            investigation.status = InvestigationStatus.UNDER_REVIEW
            write_audit_log(
                db=self.db,
                action="INVESTIGATION_UNDER_REVIEW",
                entity_type="Investigation",
                entity_id=str(investigation.id),
                actor_role=actor.role.value,
                actor_id=actor.id,
                claim_id=claim_id,
                event_data={"previous_status": InvestigationStatus.ASSIGNED.value},
            )
            self.db.commit()
            self.db.refresh(investigation)

        return investigation

    def add_finding(
        self,
        claim_id: uuid.UUID,
        finding_text: str,
        actor: User,
    ) -> InvestigationFinding:
        """Add an append-only finding to an active investigation."""
        clean_text = finding_text.strip()
        if len(clean_text) < 5:
            raise InvestigationValidationError("Finding text must be at least 5 characters long")

        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise InvestigationServiceError(f"Claim '{claim_id}' not found")
        check_claim_access(actor, claim, action="MUTATE")

        investigation = self.get_active_investigation(claim_id)

        if actor.role not in (UserRole.MANAGER, UserRole.ADMIN):
            if investigation.assigned_to != actor.id:
                raise InvestigationPermissionError(
                    "Only the assigned investigator, manager, or admin can record findings"
                )

        # Auto-advance to UNDER_REVIEW if still ASSIGNED
        if investigation.status == InvestigationStatus.ASSIGNED:
            investigation.status = InvestigationStatus.UNDER_REVIEW

        finding_id = uuid.uuid4()
        finding = InvestigationFinding(
            id=finding_id,
            investigation_id=investigation.id,
            author_id=actor.id,
            finding_text=clean_text,
            created_at=utc_now(),
        )
        self.db.add(finding)
        investigation.findings.append(finding)

        write_audit_log(
            db=self.db,
            action="INVESTIGATION_FINDING_ADDED",
            entity_type="InvestigationFinding",
            entity_id=str(finding_id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim_id,
            event_data={"findings_sha256": hash_free_text(clean_text)},
        )

        self.db.commit()
        self.db.refresh(finding)
        return finding

    def escalate_investigation(
        self,
        claim_id: uuid.UUID,
        reason: str,
        actor: User,
    ) -> Investigation:
        """Escalate an investigation to managerial attention and update claim status to ESCALATED."""
        clean_reason = reason.strip()
        if len(clean_reason) < 5:
            raise InvestigationValidationError(
                "Escalation reason must be at least 5 characters long"
            )

        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise InvestigationServiceError(f"Claim '{claim_id}' not found")
        check_claim_access(actor, claim, action="MUTATE")

        investigation = self.get_active_investigation(claim_id)

        if actor.role not in (UserRole.MANAGER, UserRole.ADMIN):
            if investigation.assigned_to != actor.id:
                raise InvestigationPermissionError(
                    "Only the assigned investigator, manager, or admin can escalate this investigation"
                )

        investigation.status = InvestigationStatus.ESCALATED

        if claim.status == ClaimStatus.UNDER_INVESTIGATION:
            validate_transition(claim.status, ClaimStatus.ESCALATED)
            claim.status = ClaimStatus.ESCALATED
            claim.status_version += 1

            history = ClaimStatusHistory(
                claim_id=claim.id,
                from_status=ClaimStatus.UNDER_INVESTIGATION.value,
                to_status=ClaimStatus.ESCALATED.value,
                changed_by=actor.id,
                reason=f"Investigation escalated: {clean_reason}",
                created_at=utc_now(),
            )
            self.db.add(history)

        write_audit_log(
            db=self.db,
            action="INVESTIGATION_ESCALATED",
            entity_type="Investigation",
            entity_id=str(investigation.id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim_id,
            event_data={"reason_sha256": hash_free_text(clean_reason)},
        )

        self.db.commit()
        self.db.refresh(investigation)
        return investigation

    def complete_investigation(
        self,
        claim_id: uuid.UUID,
        outcome: InvestigationOutcome,
        actor: User,
        remarks: str | None = None,
    ) -> Investigation:
        """Complete an investigation, verify findings exist, and transition claim to READY_FOR_ASSESSMENT."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise InvestigationServiceError(f"Claim '{claim_id}' not found")
        check_claim_access(actor, claim, action="MUTATE")

        investigation = self.get_active_investigation(claim_id)

        # Section 11: ESCALATED goes to a manager, who resolves it to COMPLETED
        if investigation.status == InvestigationStatus.ESCALATED:
            if actor.role not in (UserRole.MANAGER, UserRole.ADMIN):
                raise InvestigationPermissionError(
                    "Only a manager or admin can resolve an escalated investigation"
                )
        else:
            if actor.role not in (UserRole.MANAGER, UserRole.ADMIN):
                if investigation.assigned_to != actor.id:
                    raise InvestigationPermissionError(
                        "Only the assigned investigator, manager, or admin can complete this investigation"
                    )

        # Section 11: Completion requires at least one finding
        if not investigation.findings:
            raise InvestigationValidationError(
                "Investigation completion requires at least one finding to be recorded"
            )

        investigation.status = InvestigationStatus.COMPLETED
        investigation.outcome = outcome
        investigation.closed_at = utc_now()

        # Advance state machine:
        # Step 1: ESCALATED or UNDER_INVESTIGATION -> INVESTIGATION_COMPLETE
        if claim.status in (ClaimStatus.UNDER_INVESTIGATION, ClaimStatus.ESCALATED):
            from_status = claim.status
            validate_transition(from_status, ClaimStatus.INVESTIGATION_COMPLETE)
            claim.status = ClaimStatus.INVESTIGATION_COMPLETE
            claim.status_version += 1

            hist_step1 = ClaimStatusHistory(
                claim_id=claim.id,
                from_status=from_status.value,
                to_status=ClaimStatus.INVESTIGATION_COMPLETE.value,
                changed_by=actor.id,
                reason=f"Investigation concluded with outcome '{outcome.value}'",
                created_at=utc_now(),
            )
            self.db.add(hist_step1)

            # Step 2: INVESTIGATION_COMPLETE -> READY_FOR_ASSESSMENT
            validate_transition(claim.status, ClaimStatus.READY_FOR_ASSESSMENT)
            claim.status = ClaimStatus.READY_FOR_ASSESSMENT
            claim.status_version += 1

            hist_step2 = ClaimStatusHistory(
                claim_id=claim.id,
                from_status=ClaimStatus.INVESTIGATION_COMPLETE.value,
                to_status=ClaimStatus.READY_FOR_ASSESSMENT.value,
                changed_by=actor.id,
                reason="Investigation completed; routed to assessment queue",
                created_at=utc_now(),
            )
            self.db.add(hist_step2)

        write_audit_log(
            db=self.db,
            action="INVESTIGATION_COMPLETED",
            entity_type="Investigation",
            entity_id=str(investigation.id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim_id,
            event_data={
                "outcome": outcome.value,
                "findings_count": len(investigation.findings),
                "remarks_sha256": hash_free_text(remarks),
            },
        )

        self.db.commit()
        self.db.refresh(investigation)
        return investigation

    def get_investigations(self, claim_id: uuid.UUID, actor: User) -> list[Investigation]:
        """List all investigations and their findings for a claim."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise InvestigationServiceError(f"Claim '{claim_id}' not found")

        check_claim_access(actor, claim)

        stmt = (
            select(Investigation)
            .where(Investigation.claim_id == claim.id)
            .options(selectinload(Investigation.findings))
            .order_by(Investigation.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())

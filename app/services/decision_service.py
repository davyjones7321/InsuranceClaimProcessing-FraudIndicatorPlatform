"""Decision service orchestrating claim approvals, rejections, threshold rules, and separation of duties."""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.config import settings
from app.db.base import utc_now
from app.db.models import (
    Assessment,
    Claim,
    ClaimStatusHistory,
    Coverage,
    CoverageLedger,
    Decision,
    Investigation,
    User,
)
from app.domain.constants import ALLOWED_REJECTION_REASON_CODES
from app.domain.enums import (
    ClaimStatus,
    CoverageLedgerEntryType,
    DecisionType,
    InvestigationStatus,
    UserRole,
)
from app.domain.state_machine import validate_transition
from app.schemas.decisions import DecisionCreate
from app.security.rbac import check_claim_access


class DecisionServiceError(Exception):
    """Base error for claim decision operations."""

    pass


class DecisionValidationError(DecisionServiceError):
    """Raised when decision parameters or business invariants fail."""

    pass


class DecisionPermissionError(DecisionServiceError):
    """Raised when actor violates role or separation of duties constraints."""

    pass


class DecisionService:
    """Service handling claim decisions, separation of duties, and coverage ledger updates."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def make_decision(
        self,
        claim_id: uuid.UUID,
        payload: DecisionCreate,
        actor: User,
    ) -> Decision:
        """Execute a claim decision (APPROVED, REJECTED, INFO_REQUESTED) with strict invariant checks."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise DecisionServiceError(f"Claim '{claim_id}' not found")

        # 1. Role verification: only Staff, Manager, or Admin
        if actor.role not in (UserRole.STAFF, UserRole.MANAGER, UserRole.ADMIN):
            raise DecisionPermissionError(
                f"Role '{actor.role.value}' is not authorized to make claim decisions"
            )

        # 2. Separation of duties: Claimant cannot decide own claim
        if claim.registered_by == actor.id:
            raise DecisionPermissionError(
                "Separation of duties violation: user who registered this claim cannot decide it"
            )
        if claim.customer and claim.customer.user and claim.customer.user.id == actor.id:
            raise DecisionPermissionError(
                "Separation of duties violation: claimant cannot decide own claim"
            )

        # 3. Separation of duties: Investigator assigned to this claim cannot decide it
        inv_assigned_stmt = select(Investigation).where(
            Investigation.claim_id == claim.id,
            Investigation.assigned_to == actor.id,
        )
        if self.db.scalars(inv_assigned_stmt).first():
            raise DecisionPermissionError(
                "Separation of duties violation: assigned investigator for this claim cannot decide it"
            )

        # 4. Mandatory remarks constraint
        clean_remarks = payload.remarks.strip()
        if len(clean_remarks) < settings.MIN_DECISION_REMARKS_LENGTH:
            raise DecisionValidationError(
                f"Decision remarks must be at least {settings.MIN_DECISION_REMARKS_LENGTH} characters long"
            )

        # 5. Open investigation blocking constraint
        open_inv_stmt = select(Investigation).where(
            Investigation.claim_id == claim.id,
            Investigation.status != InvestigationStatus.COMPLETED,
        )
        if self.db.scalars(open_inv_stmt).first():
            raise DecisionValidationError(
                "Claim cannot be decided while an active investigation is open"
            )

        # Fetch latest assessment if one exists
        latest_assessment_stmt = (
            select(Assessment)
            .where(Assessment.claim_id == claim.id)
            .order_by(Assessment.created_at.desc())
        )
        assessment = self.db.scalars(latest_assessment_stmt).first()

        # 6. Branching per decision type
        target_status: ClaimStatus
        if payload.decision == DecisionType.APPROVED:
            target_status = ClaimStatus.APPROVED

            # Status constraint: Must be in ASSESSED status
            if claim.status != ClaimStatus.ASSESSED:
                raise DecisionValidationError(
                    f"Claim in status '{claim.status.value}' cannot be approved. Must be 'ASSESSED'."
                )

            # Assessment prerequisite
            if not assessment:
                raise DecisionValidationError("Approval requires an assessment to exist")

            # Separation of duties: Assessor cannot decide
            if assessment.assessor_id == actor.id:
                raise DecisionPermissionError(
                    "Separation of duties violation: assessor who assessed this claim cannot decide it"
                )

            # Approved amount checks
            if payload.approved_amount is None or payload.approved_amount <= Decimal("0.00"):
                raise DecisionValidationError(
                    "Approval decision requires a positive approved_amount"
                )

            # Cap check: cannot exceed assessed eligible amount
            if payload.approved_amount > assessment.eligible_amount:
                raise DecisionValidationError(
                    f"Approved amount ({payload.approved_amount}) cannot exceed assessed eligible amount ({assessment.eligible_amount})"
                )

            # Manager threshold check
            if payload.approved_amount > settings.MANAGER_APPROVAL_THRESHOLD:
                if actor.role not in (UserRole.MANAGER, UserRole.ADMIN):
                    raise DecisionPermissionError(
                        f"Approval of amount {payload.approved_amount} exceeds staff threshold "
                        f"{settings.MANAGER_APPROVAL_THRESHOLD}; manager or admin role required"
                    )

            # Consume coverage limit and write to ledger
            coverage_stmt = select(Coverage).where(
                Coverage.policy_id == claim.policy_id,
                Coverage.claim_category == claim.claim_category,
            )
            coverage = self.db.scalars(coverage_stmt).first()
            if coverage:
                coverage.consumed_amount += payload.approved_amount
                ledger_entry = CoverageLedger(
                    id=uuid.uuid4(),
                    coverage_id=coverage.id,
                    claim_id=claim.id,
                    entry_type=CoverageLedgerEntryType.CONSUME,
                    amount=payload.approved_amount,
                    reason=f"Claim {claim.claim_ref} approved",
                    created_at=utc_now(),
                )
                self.db.add(ledger_entry)

        elif payload.decision == DecisionType.REJECTED:
            target_status = ClaimStatus.REJECTED

            # Separation of duties: If assessment exists, assessor cannot reject
            if assessment and assessment.assessor_id == actor.id:
                raise DecisionPermissionError(
                    "Separation of duties violation: assessor who assessed this claim cannot decide it"
                )

            # Rejection reason codes mandatory and from allowlist
            if not payload.reason_codes:
                raise DecisionValidationError(
                    "REJECTED decision requires at least one reason code from the allowed list"
                )

            for code in payload.reason_codes:
                if code not in ALLOWED_REJECTION_REASON_CODES:
                    raise DecisionValidationError(
                        f"Reason code '{code}' is not in the allowed rejection reasons list"
                    )

        elif payload.decision == DecisionType.INFO_REQUESTED:
            target_status = ClaimStatus.INFO_REQUESTED

            # Separation of duties: If assessment exists, assessor cannot decide
            if assessment and assessment.assessor_id == actor.id:
                raise DecisionPermissionError(
                    "Separation of duties violation: assessor who assessed this claim cannot decide it"
                )

        else:
            raise DecisionValidationError(f"Unsupported decision type '{payload.decision}'")

        # Validate state machine transition
        validate_transition(claim.status, target_status)

        decision_id = uuid.uuid4()
        decision_record = Decision(
            id=decision_id,
            claim_id=claim.id,
            decided_by=actor.id,
            decision=payload.decision,
            approved_amount=payload.approved_amount,
            remarks=clean_remarks,
            reason_codes=payload.reason_codes,
            created_at=utc_now(),
        )
        self.db.add(decision_record)

        # Update claim status
        old_status = claim.status
        claim.status = target_status
        claim.status_version += 1

        history = ClaimStatusHistory(
            claim_id=claim.id,
            from_status=old_status.value,
            to_status=target_status.value,
            changed_by=actor.id,
            reason=f"Decision '{payload.decision.value}': {clean_remarks[:100]}",
            created_at=utc_now(),
        )
        self.db.add(history)

        write_audit_log(
            db=self.db,
            action="CLAIM_DECISION",
            entity_type="Decision",
            entity_id=str(decision_id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim.id,
            event_data={
                "decision": payload.decision.value,
                "approved_amount": str(payload.approved_amount) if payload.approved_amount else None,
                "reason_codes": payload.reason_codes,
                "remarks": clean_remarks,
            },
        )

        self.db.commit()
        self.db.refresh(decision_record)
        return decision_record

    def get_decisions(self, claim_id: uuid.UUID, actor: User) -> list[Decision]:
        """Retrieve all decisions recorded for a claim."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise DecisionServiceError(f"Claim '{claim_id}' not found")

        check_claim_access(actor, claim)

        stmt = (
            select(Decision)
            .where(Decision.claim_id == claim.id)
            .order_by(Decision.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())

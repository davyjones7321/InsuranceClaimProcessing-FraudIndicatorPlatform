"""Assessment service orchestrating cap calculations, separation of duties, and state transitions."""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.db.base import utc_now
from app.db.models import Assessment, Claim, ClaimStatusHistory, Coverage, Investigation, User
from app.domain.enums import ClaimStatus, InvestigationStatus, UserRole
from app.domain.state_machine import validate_transition
from app.engine.assessment_cap import calculate_assessment_cap
from app.engine.types import AssessmentInput
from app.schemas.assessments import AssessmentCreate
from app.security.rbac import check_claim_access


class AssessmentServiceError(Exception):
    """Base error for assessment operations."""

    pass


class AssessmentValidationError(AssessmentServiceError):
    """Raised when assessment input or business constraint fails validation."""

    pass


class AssessmentPermissionError(AssessmentServiceError):
    """Raised when actor violates role or separation of duties constraints."""

    pass


class AssessmentService:
    """Service handling claim assessments, cap calculation, and status progression."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_assessment(
        self,
        claim_id: uuid.UUID,
        payload: AssessmentCreate,
        actor: User,
    ) -> Assessment:
        """Evaluate engine cap, verify separation of duties, record assessment and advance claim to ASSESSED."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise AssessmentServiceError(f"Claim '{claim_id}' not found")

        # Role constraint: only Assessor, Manager, or Admin
        if actor.role not in (UserRole.ASSESSOR, UserRole.MANAGER, UserRole.ADMIN):
            raise AssessmentPermissionError(
                f"Role '{actor.role.value}' is not authorized to submit assessments"
            )

        # Separation of duties: claimant cannot assess own claim
        if claim.registered_by == actor.id:
            raise AssessmentPermissionError(
                "Separation of duties violation: user who registered this claim cannot assess it"
            )
        if claim.customer and claim.customer.user and claim.customer.user.id == actor.id:
            raise AssessmentPermissionError(
                "Separation of duties violation: claimant cannot assess own claim"
            )

        # Separation of duties: investigator assigned to claim cannot assess it
        open_or_prior_inv_stmt = select(Investigation).where(
            Investigation.claim_id == claim.id,
            Investigation.assigned_to == actor.id,
        )
        if self.db.scalars(open_or_prior_inv_stmt).first():
            raise AssessmentPermissionError(
                "Separation of duties violation: assigned investigator for this claim cannot assess it"
            )

        # Claim status constraint: claim must be READY_FOR_ASSESSMENT
        if claim.status != ClaimStatus.READY_FOR_ASSESSMENT:
            raise AssessmentValidationError(
                f"Claim is in status '{claim.status.value}'. Must be 'READY_FOR_ASSESSMENT' to assess."
            )

        # Investigation blocking constraint: cannot assess while an investigation is open
        open_inv_stmt = select(Investigation).where(
            Investigation.claim_id == claim.id,
            Investigation.status != InvestigationStatus.COMPLETED,
        )
        if self.db.scalars(open_inv_stmt).first():
            raise AssessmentValidationError(
                "Claim cannot be assessed while an active investigation is open"
            )

        # Fetch coverage row
        coverage_stmt = select(Coverage).where(
            Coverage.policy_id == claim.policy_id,
            Coverage.claim_category == claim.claim_category,
        )
        coverage = self.db.scalars(coverage_stmt).first()
        if not coverage:
            raise AssessmentValidationError(
                f"No coverage row found for category '{claim.claim_category}' on policy '{claim.policy_id}'"
            )

        remaining_limit = max(Decimal("0.00"), coverage.limit_amount - coverage.consumed_amount)
        policy = claim.policy

        deductible = (
            payload.deductible if payload.deductible is not None else coverage.deductible
        )
        copay_percent = (
            payload.copay_percent
            if payload.copay_percent is not None
            else coverage.copay_percent
        )
        depreciation = (
            payload.depreciation if payload.depreciation is not None else Decimal("0.00")
        )
        sum_insured = policy.sum_insured
        value_at_risk = (
            payload.value_at_risk if payload.value_at_risk is not None else Decimal("0.00")
        )

        assessment_input = AssessmentInput(
            product_type=policy.product_type.value,
            requested_amount=claim.requested_amount,
            remaining_limit=remaining_limit,
            assessed_loss=payload.assessed_loss,
            deductible=deductible,
            copay_percent=copay_percent,
            depreciation=depreciation,
            sum_insured=sum_insured,
            value_at_risk=value_at_risk,
        )

        # Pure deterministic cap calculation
        cap_output = calculate_assessment_cap(assessment_input)

        # Business rule: eligible_amount can never exceed the engine cap
        if payload.eligible_amount > cap_output.max_eligible_amount:
            raise AssessmentValidationError(
                f"Eligible amount {payload.eligible_amount} exceeds maximum eligible cap {cap_output.max_eligible_amount}"
            )

        # State machine transition: READY_FOR_ASSESSMENT -> ASSESSED
        validate_transition(claim.status, ClaimStatus.ASSESSED)

        assessment_id = uuid.uuid4()
        assessment = Assessment(
            id=assessment_id,
            claim_id=claim.id,
            assessor_id=actor.id,
            assessed_loss=payload.assessed_loss,
            max_eligible_amount=cap_output.max_eligible_amount,
            eligible_amount=payload.eligible_amount,
            calculation_trace=cap_output.calculation_trace,
            remarks=payload.remarks,
            supporting_info=payload.supporting_info,
            created_at=utc_now(),
        )
        self.db.add(assessment)

        # Update claim status and optimistic version
        claim.status = ClaimStatus.ASSESSED
        claim.status_version += 1

        history = ClaimStatusHistory(
            claim_id=claim.id,
            from_status=ClaimStatus.READY_FOR_ASSESSMENT.value,
            to_status=ClaimStatus.ASSESSED.value,
            changed_by=actor.id,
            reason=f"Assessment recorded: eligible {payload.eligible_amount} (cap {cap_output.max_eligible_amount})",
            created_at=utc_now(),
        )
        self.db.add(history)

        write_audit_log(
            db=self.db,
            action="CLAIM_ASSESSED",
            entity_type="Assessment",
            entity_id=str(assessment_id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim.id,
            event_data={
                "assessed_loss": str(payload.assessed_loss),
                "eligible_amount": str(payload.eligible_amount),
                "max_eligible_amount": str(cap_output.max_eligible_amount),
                "remarks": payload.remarks,
            },
        )

        self.db.commit()
        self.db.refresh(assessment)
        return assessment

    def get_assessments(self, claim_id: uuid.UUID, actor: User) -> list[Assessment]:
        """Retrieve all assessments for a claim subject to object-level access controls."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise AssessmentServiceError(f"Claim '{claim_id}' not found")

        check_claim_access(actor, claim)

        stmt = (
            select(Assessment)
            .where(Assessment.claim_id == claim.id)
            .order_by(Assessment.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())

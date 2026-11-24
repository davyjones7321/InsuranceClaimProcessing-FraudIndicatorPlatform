"""Decision service orchestrating claim approvals, rejections, threshold rules, and separation of duties."""

import uuid
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit.schema import hash_free_text
from app.audit.writer import write_audit_log
from app.config import settings
from app.db.base import utc_now
from app.db.lock_utils import set_lock_timeout, with_concurrency_retry
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
    LimitScope,
    UserRole,
)
from app.domain.state_machine import validate_transition
from app.engine.assessment_cap import calculate_assessment_cap
from app.engine.types import AssessmentInput
from app.schemas.decisions import DecisionCreate
from app.security.rbac import (
    PermissionDeniedError,
    check_claim_access,
)
from app.services.claim_service import StaleStatusVersionError


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

    @with_concurrency_retry()
    def make_decision(
        self,
        claim_id: uuid.UUID,
        payload: DecisionCreate,
        actor: User,
    ) -> Decision:
        """Execute a claim decision (APPROVED, REJECTED, INFO_REQUESTED) with strict invariant checks.

        Lock ordering:
        1. Claim row (FOR UPDATE)
        2. Coverage row sorted by ID (FOR UPDATE)
        3. Assessment row (FOR UPDATE)
        """
        set_lock_timeout(self.db)

        # 1. Row-level lock on Claim
        claim_stmt = select(Claim).where(Claim.id == claim_id)
        bind = self.db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            claim_stmt = claim_stmt.with_for_update()

        claim = self.db.scalars(claim_stmt).first()
        if not claim:
            raise DecisionServiceError(f"Claim '{claim_id}' not found")

        # Optimistic locking token check
        if (
            payload.expected_status_version is not None
            and claim.status_version != payload.expected_status_version
        ):
            raise StaleStatusVersionError(
                f"Stale claim state: expected status_version {payload.expected_status_version}, current is {claim.status_version}"
            )

        # 2. Service-level object authorization
        try:
            check_claim_access(actor, claim, action="DECIDE")
        except PermissionDeniedError as exc:
            raise DecisionPermissionError(str(exc)) from exc

        # 3. Role verification: only Staff or Manager (D6: Admin forbidden from making claim decisions)
        if actor.role not in (UserRole.STAFF, UserRole.MANAGER):
            raise DecisionPermissionError(
                f"Role '{actor.role.value}' is not authorized to make claim decisions (D6)"
            )

        # 4. Separation of duties: Claimant cannot decide own claim
        if claim.registered_by == actor.id:
            raise DecisionPermissionError(
                "Separation of duties violation: user who registered this claim cannot decide it"
            )
        if claim.customer and claim.customer.user and claim.customer.user.id == actor.id:
            raise DecisionPermissionError(
                "Separation of duties violation: claimant cannot decide own claim"
            )

        # 5. Separation of duties: Investigator assigned to this claim cannot decide it
        inv_assigned_stmt = select(Investigation).where(
            Investigation.claim_id == claim.id,
            Investigation.assigned_to == actor.id,
        )
        if self.db.scalars(inv_assigned_stmt).first():
            raise DecisionPermissionError(
                "Separation of duties violation: assigned investigator for this claim cannot decide it"
            )

        # 5b. Separation of duties: Assessor cannot decide it
        for asm in claim.assessments:
            if asm.assessor_id == actor.id:
                raise DecisionPermissionError(
                    "Separation of duties violation: assessor who assessed this claim cannot decide it"
                )

        # 5c. Separation of duties (P6 Task 9): Confirmer of claim's incident group cannot decide it
        if claim.incident_group_id is not None:
            from app.db.models import IncidentGroup

            group = self.db.get(IncidentGroup, claim.incident_group_id)
            if group and group.confirmed_by_id == actor.id:
                raise DecisionPermissionError(
                    "Separation of duties violation: incident group confirmer cannot decide claims in the group"
                )

        # 6. Mandatory remarks constraint
        clean_remarks = payload.remarks.strip()
        if len(clean_remarks) < settings.MIN_DECISION_REMARKS_LENGTH:
            raise DecisionValidationError(
                f"Decision remarks must be at least {settings.MIN_DECISION_REMARKS_LENGTH} characters long"
            )

        # 7. Open investigation blocking constraint
        open_inv_stmt = select(Investigation).where(
            Investigation.claim_id == claim.id,
            Investigation.status != InvestigationStatus.COMPLETED,
        )
        if self.db.scalars(open_inv_stmt).first():
            raise DecisionValidationError(
                "Claim cannot be decided while an active investigation is open"
            )

        # Fetch latest non-superseded assessment
        latest_assessment_stmt = (
            select(Assessment)
            .where(Assessment.claim_id == claim.id, Assessment.is_superseded == False)  # noqa: E712
            .order_by(Assessment.created_at.desc())
        )
        if bind is not None and bind.dialect.name == "postgresql":
            latest_assessment_stmt = latest_assessment_stmt.with_for_update()
        assessment = self.db.scalars(latest_assessment_stmt).first()

        # 8. Branching per decision type
        target_status: ClaimStatus
        decision_id = uuid.uuid4()
        limit_applied_value: Decimal | None = None
        reason_codes_list: list[str] = list(payload.reason_codes or [])

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

            # Lock Coverage row (sorted by id)
            coverage_stmt = (
                select(Coverage)
                .where(
                    Coverage.policy_id == claim.policy_id,
                    Coverage.claim_category == claim.claim_category,
                )
                .order_by(Coverage.id)
            )
            if bind is not None and bind.dialect.name == "postgresql":
                coverage_stmt = coverage_stmt.with_for_update()

            coverage = self.db.scalars(coverage_stmt).first()
            if not coverage:
                raise DecisionValidationError(
                    f"No coverage row found for category '{claim.claim_category}' on policy '{claim.policy_id}'"
                )

            # Recompute remaining capacity by limit_scope
            if coverage.limit_scope == LimitScope.PER_CLAIM:
                remaining_capacity = coverage.limit_amount
            else:
                remaining_capacity = max(
                    Decimal("0.00"), coverage.limit_amount - coverage.consumed_amount
                )

            limit_applied_value = remaining_capacity

            # Recompute payable P' from stored assessed loss using the engine
            policy = claim.policy
            trace = assessment.calculation_trace
            deductible = Decimal(trace.get("deductible", str(coverage.deductible)))
            copay_pct = Decimal(trace.get("copay_percent", str(coverage.copay_percent)))
            depr = Decimal(trace.get("depreciation", "0.00"))
            val_risk = Decimal(trace.get("value_at_risk", "0.00"))

            assessment_input = AssessmentInput(
                product_type=policy.product_type.value,
                requested_amount=claim.requested_amount,
                remaining_limit=remaining_capacity,
                assessed_loss=assessment.assessed_loss,
                deductible=deductible,
                copay_percent=copay_pct,
                depreciation=depr,
                sum_insured=policy.sum_insured,
                value_at_risk=val_risk,
            )
            cap_output = calculate_assessment_cap(assessment_input)
            p_prime = cap_output.max_eligible_amount

            # Require 0 < approved <= P'
            if payload.approved_amount > p_prime:
                raise DecisionValidationError(
                    f"Approved amount ({payload.approved_amount}) cannot exceed assessed eligible amount (exceeds remaining allowable payable limit {p_prime})"
                )

            # Check if limit was applied (uncapped exceeds remaining_capacity)
            uncapped_input = AssessmentInput(
                product_type=policy.product_type.value,
                requested_amount=claim.requested_amount,
                remaining_limit=Decimal("999999999.99"),
                assessed_loss=assessment.assessed_loss,
                deductible=deductible,
                copay_percent=copay_pct,
                depreciation=depr,
                sum_insured=policy.sum_insured,
                value_at_risk=val_risk,
            )
            uncapped_output = calculate_assessment_cap(uncapped_input)
            is_capped = uncapped_output.max_eligible_amount > remaining_capacity

            if is_capped:
                if "LIMIT_APPLIED" not in reason_codes_list:
                    reason_codes_list.append("LIMIT_APPLIED")
                if settings.REQUIRE_MANAGER_WHEN_CAPPED and actor.role not in (
                    UserRole.MANAGER,
                    UserRole.ADMIN,
                ):
                    raise DecisionPermissionError(
                        "Approval with capped coverage limit requires manager role"
                    )

            # Manager threshold check
            if payload.approved_amount > settings.MANAGER_APPROVAL_THRESHOLD:
                if actor.role not in (UserRole.MANAGER, UserRole.ADMIN):
                    raise DecisionPermissionError(
                        f"Approval of amount {payload.approved_amount} exceeds staff threshold "
                        f"{settings.MANAGER_APPROVAL_THRESHOLD}; manager or admin role required"
                    )

            # Update consumed_amount using SQL expression under lock
            self.db.execute(
                update(Coverage)
                .where(Coverage.id == coverage.id)
                .values(consumed_amount=Coverage.consumed_amount + payload.approved_amount)
            )
            self.db.expire(coverage, ["consumed_amount"])

            # Insert CONSUME ledger entry
            ledger_entry = CoverageLedger(
                id=uuid.uuid4(),
                coverage_id=coverage.id,
                claim_id=claim.id,
                decision_id=decision_id,
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

        decision_record = Decision(
            id=decision_id,
            claim_id=claim.id,
            decided_by=actor.id,
            decision=payload.decision,
            approved_amount=payload.approved_amount,
            limit_applied=limit_applied_value,
            remarks=clean_remarks,
            reason_codes=reason_codes_list if reason_codes_list else None,
            created_at=utc_now(),
        )
        self.db.add(decision_record)

        # Update claim status
        old_status = claim.status
        claim.status = target_status
        claim.status_version += 1

        history = ClaimStatusHistory(
            id=uuid.uuid4(),
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
                "approved_amount": str(payload.approved_amount)
                if payload.approved_amount
                else None,
                "reason_codes": reason_codes_list if reason_codes_list else None,
                "remarks_sha256": hash_free_text(clean_remarks),
            },
        )

        self.db.commit()
        self.db.refresh(decision_record)
        return decision_record

    @with_concurrency_retry()
    def reopen_claim(
        self,
        claim_id: uuid.UUID,
        actor: User,
        remarks: str,
        expected_status_version: int | None = None,
    ) -> Claim:
        """Manager-only reopen of an APPROVED or REJECTED claim to READY_FOR_ASSESSMENT.

        Per Decision D1 and Task 7:
        - Manager role required.
        - Mandatory remarks (min length checked).
        - If reopening an APPROVED claim: compensating REVERSE ledger row created referencing the CONSUME row,
          and Coverage.consumed_amount decremented via SQL expression under row lock.
        - Prior active assessment(s) marked superseded (is_superseded = True).
        - Original decision and ledger rows are NEVER edited (append-only).
        - Transitions claim status to READY_FOR_ASSESSMENT.
        - Increments status_version and writes status history and audit log.
        """
        set_lock_timeout(self.db)

        # 1. Role verification: only Manager or Admin
        if actor.role not in (UserRole.MANAGER, UserRole.ADMIN):
            raise DecisionPermissionError(
                f"Role '{actor.role.value}' is not authorized to reopen finalized claims. Manager role required (D1)."
            )

        # 2. Lock Claim row
        claim_stmt = select(Claim).where(Claim.id == claim_id)
        bind = self.db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            claim_stmt = claim_stmt.with_for_update()
        claim = self.db.scalars(claim_stmt).first()
        if not claim:
            raise DecisionServiceError(f"Claim '{claim_id}' not found")

        # 3. Object-level access check
        try:
            check_claim_access(actor, claim, action="MUTATE")
        except PermissionDeniedError as exc:
            raise DecisionPermissionError(str(exc)) from exc

        # 4. Optimistic locking token check
        if expected_status_version is not None and claim.status_version != expected_status_version:
            raise StaleStatusVersionError(
                f"Stale claim state: expected status_version {expected_status_version}, current is {claim.status_version}"
            )

        # 5. Status check: must be in APPROVED or REJECTED
        if claim.status not in (ClaimStatus.APPROVED, ClaimStatus.REJECTED):
            raise DecisionValidationError(
                f"Cannot reopen claim in status '{claim.status.value}'. Must be 'APPROVED' or 'REJECTED'."
            )

        # 6. Mandatory remarks check
        clean_remarks = remarks.strip() if remarks else ""
        if len(clean_remarks) < settings.MIN_DECISION_REMARKS_LENGTH:
            raise DecisionValidationError(
                f"Reopen remarks must be at least {settings.MIN_DECISION_REMARKS_LENGTH} characters long"
            )

        # 7. Validate state machine transition
        validate_transition(claim.status, ClaimStatus.READY_FOR_ASSESSMENT)

        old_status = claim.status

        # 8. If reopening an APPROVED claim, reverse consumed coverage under row lock
        if old_status == ClaimStatus.APPROVED:
            # Lock coverage row sorted by id
            coverage_stmt = (
                select(Coverage)
                .where(
                    Coverage.policy_id == claim.policy_id,
                    Coverage.claim_category == claim.claim_category,
                )
                .order_by(Coverage.id)
            )
            if bind is not None and bind.dialect.name == "postgresql":
                coverage_stmt = coverage_stmt.with_for_update()
            coverage = self.db.scalars(coverage_stmt).first()

            # Find the CONSUME ledger row for this claim
            consume_stmt = (
                select(CoverageLedger)
                .where(
                    CoverageLedger.claim_id == claim.id,
                    CoverageLedger.entry_type == CoverageLedgerEntryType.CONSUME,
                )
                .order_by(CoverageLedger.created_at.desc())
            )
            consume_entry = self.db.scalars(consume_stmt).first()
            if consume_entry:
                reverse_ledger = CoverageLedger(
                    id=uuid.uuid4(),
                    coverage_id=consume_entry.coverage_id,
                    claim_id=claim.id,
                    decision_id=None,
                    parent_ledger_id=consume_entry.id,
                    entry_type=CoverageLedgerEntryType.REVERSE,
                    amount=consume_entry.amount,
                    reason=f"Manager reopen reversal: {clean_remarks}",
                    created_at=utc_now(),
                )
                self.db.add(reverse_ledger)

                # Decrement consumed_amount under lock via SQL expression
                if coverage:
                    self.db.execute(
                        update(Coverage)
                        .where(Coverage.id == coverage.id)
                        .values(consumed_amount=Coverage.consumed_amount - consume_entry.amount)
                    )
                    self.db.expire(coverage, ["consumed_amount"])

        # 9. Supersede prior active assessment(s)
        self.db.execute(
            update(Assessment)
            .where(Assessment.claim_id == claim.id, Assessment.is_superseded == False)  # noqa: E712
            .values(is_superseded=True)
        )

        # 10. Advance claim status
        claim.status = ClaimStatus.READY_FOR_ASSESSMENT
        claim.status_version += 1

        # 11. Record status history
        history = ClaimStatusHistory(
            id=uuid.uuid4(),
            claim_id=claim.id,
            from_status=old_status.value,
            to_status=ClaimStatus.READY_FOR_ASSESSMENT.value,
            changed_by=actor.id,
            reason=f"Manager reopen: {clean_remarks[:100]}",
            created_at=utc_now(),
        )
        self.db.add(history)

        # 12. Write audit log
        write_audit_log(
            db=self.db,
            action="REVERSAL",
            entity_type="Claim",
            entity_id=str(claim.id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim.id,
            event_data={
                "action": "REOPEN",
                "from_status": old_status.value,
                "to_status": ClaimStatus.READY_FOR_ASSESSMENT.value,
                "remarks_sha256": hash_free_text(clean_remarks),
            },
        )

        self.db.commit()
        self.db.refresh(claim)
        return claim

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

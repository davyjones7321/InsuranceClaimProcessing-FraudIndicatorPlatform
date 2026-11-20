"""Domain orchestration service for claim validation against active ruleset."""

import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.schema import hash_free_text
from app.audit.writer import write_audit_log
from app.config import settings
from app.db.base import utc_now
from app.db.models import (
    Claim,
    ClaimDocument,
    ClaimStatusHistory,
    Coverage,
    Exclusion,
    Policy,
    RequiredDocumentType,
    User,
    ValidationResult,
    ValidationRun,
)
from app.domain.enums import (
    ClaimStatus,
    RuleOutcome,
    ValidationOutcome,
)
from app.domain.state_machine import validate_transition
from app.engine.types import (
    ClaimInput,
    CoverageInput,
    DocumentInput,
    ExclusionInput,
    PolicyInput,
    RequiredDocumentTypeInput,
    ValidationInput,
    ValidationOutcomeEnum,
)
from app.engine.validation import evaluate_validation
from app.rules.loader import ACTIVE_RULESET
from app.security.rbac import check_claim_access


class ValidationServiceError(ValueError):
    """Raised when validation service preconditions fail."""

    pass


class ValidationService:
    """Orchestration service bridging DB persistence with pure deterministic validation engine."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def build_validation_input(
        self,
        claim: Claim,
        evaluation_date: date | None = None,
        ruleset: dict[str, Any] | None = None,
    ) -> ValidationInput:
        """Construct pure ValidationInput dataclass from database state."""
        policy = self.db.get(Policy, claim.policy_id)

        # Coverages and exclusions
        coverages = self.db.scalars(
            select(Coverage).where(Coverage.policy_id == claim.policy_id)
        ).all()
        exclusions = self.db.scalars(
            select(Exclusion).where(Exclusion.policy_id == claim.policy_id)
        ).all()

        # Documents for claim
        documents = self.db.scalars(
            select(ClaimDocument).where(ClaimDocument.claim_id == claim.id)
        ).all()

        # Required document types for the product type and category
        if policy:
            req_stmt = select(RequiredDocumentType).where(
                RequiredDocumentType.product_type == policy.product_type
            )
            if claim.claim_category:
                req_stmt = req_stmt.where(
                    (RequiredDocumentType.claim_category == claim.claim_category)
                    | (RequiredDocumentType.claim_category.is_(None))
                )
            req_docs = list(self.db.scalars(req_stmt).all())
        else:
            req_docs = []

        # Determine evaluation date in business timezone
        if evaluation_date is None:
            tz = ZoneInfo(settings.BUSINESS_TIMEZONE)
            evaluation_date = datetime.now(tz).date()

        active_rules = ruleset or ACTIVE_RULESET

        # Transform to pure dataclasses
        claim_input = ClaimInput(
            claim_id=str(claim.id),
            claim_ref=claim.claim_ref,
            customer_id=str(claim.customer_id),
            policy_id=str(claim.policy_id),
            claim_category=claim.claim_category,
            incident_date=claim.incident_date,
            incident_location=claim.incident_location,
            incident_description=claim.incident_description,
            requested_amount=claim.requested_amount,
            registered_date=claim.registered_at.date(),
            insured_item_id=str(claim.insured_item_id) if claim.insured_item_id else None,
        )

        policy_input: PolicyInput | None = None
        if policy:
            policy_input = PolicyInput(
                policy_id=str(policy.id),
                policy_no=policy.policy_no,
                customer_id=str(policy.customer_id),
                product_type=policy.product_type.value,
                start_date=policy.start_date,
                end_date=policy.end_date,
                status=policy.status.value,
            )

        coverage_inputs = tuple(
            CoverageInput(
                coverage_id=str(c.id),
                claim_category=c.claim_category,
                limit_amount=c.limit_amount,
                consumed_amount=c.consumed_amount,
                deductible=c.deductible,
                copay_percent=c.copay_percent,
                waiting_period_days=c.waiting_period_days,
                is_excluded=c.is_excluded,
                limit_exceeded_behavior=c.limit_exceeded_behavior,
                limit_scope=c.limit_scope.value
                if hasattr(c.limit_scope, "value")
                else str(c.limit_scope),
            )
            for c in coverages
        )

        exclusion_inputs = tuple(
            ExclusionInput(
                exclusion_code=e.exclusion_code,
                description=e.description,
            )
            for e in exclusions
        )

        document_inputs = tuple(
            DocumentInput(
                document_id=str(d.id),
                doc_code=d.doc_code,
                status=d.status.value,
                named_person=d.named_person,
                document_date=d.document_date,
            )
            for d in documents
        )

        req_doc_inputs = tuple(
            RequiredDocumentTypeInput(
                doc_code=r.doc_code,
                name=r.name,
                is_mandatory=r.is_mandatory,
            )
            for r in req_docs
        )

        return ValidationInput(
            claim=claim_input,
            policy=policy_input,
            coverages=coverage_inputs,
            exclusions=exclusion_inputs,
            documents=document_inputs,
            required_document_types=req_doc_inputs,
            evaluation_date=evaluation_date,
            ruleset=active_rules,
        )

    def validate_claim(
        self,
        claim_id: uuid.UUID,
        actor: User,
        evaluation_date: date | None = None,
        ruleset: dict[str, Any] | None = None,
    ) -> ValidationRun:
        """Run validation rules against claim and transition claim status transactionally."""
        stmt = select(Claim).where(Claim.id == claim_id)
        bind = self.db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()

        claim = self.db.scalars(stmt).first()
        if not claim:
            raise ValidationServiceError(f"Claim '{claim_id}' not found")

        # Object-level access verification
        check_claim_access(actor, claim)

        # State machine check:
        # If in DOCUMENTS_PENDING or VALIDATION_FAILED, transition to REGISTERED first per state machine
        if claim.status in (ClaimStatus.DOCUMENTS_PENDING, ClaimStatus.VALIDATION_FAILED):
            old_st = claim.status
            validate_transition(old_st, ClaimStatus.REGISTERED)
            claim.status = ClaimStatus.REGISTERED
            claim.status_version += 1
            history = ClaimStatusHistory(
                claim_id=claim.id,
                from_status=old_st.value,
                to_status=ClaimStatus.REGISTERED.value,
                changed_by=actor.id,
                reason="Revalidation initiated",
                created_at=utc_now(),
            )
            self.db.add(history)
            write_audit_log(
                db=self.db,
                action="CLAIM_STATUS_TRANSITION",
                entity_type="Claim",
                entity_id=str(claim.id),
                actor_role=actor.role.value,
                actor_id=actor.id,
                claim_id=claim.id,
                event_data={
                    "from_status": old_st.value,
                    "to_status": ClaimStatus.REGISTERED.value,
                    "status_version": claim.status_version,
                    "reason_sha256": hash_free_text("Revalidation initiated"),
                },
            )
            self.db.flush()
        elif claim.status != ClaimStatus.REGISTERED:
            raise ValidationServiceError(
                f"Cannot validate claim with status '{claim.status.value}'. Must be REGISTERED, DOCUMENTS_PENDING, or VALIDATION_FAILED."
            )

        # Build validation input and evaluate deterministically
        val_input = self.build_validation_input(
            claim=claim,
            evaluation_date=evaluation_date,
            ruleset=ruleset,
        )
        val_output = evaluate_validation(val_input)

        # Persist ValidationRun
        run_id = uuid.uuid4()
        validation_run = ValidationRun(
            id=run_id,
            claim_id=claim.id,
            ruleset_version=val_output.ruleset_version,
            engine_version=val_output.engine_version,
            evaluated_on=val_output.evaluated_on,
            overall_result=ValidationOutcome(val_output.overall_result.value),
            created_at=utc_now(),
        )
        self.db.add(validation_run)

        # Persist individual rule results
        for r in val_output.rule_results:
            res = ValidationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                rule_id=r.rule_id,
                rule_version=r.rule_version,
                outcome=RuleOutcome(r.outcome.value),
                reason_code=r.reason_code,
                message=r.message or "",
                inputs_snapshot=r.inputs_snapshot,
                created_at=utc_now(),
            )
            self.db.add(res)

        # Determine target state based on validation outcome
        if val_output.overall_result == ValidationOutcomeEnum.PASS:
            target_status = ClaimStatus.VALIDATED
            reason_msg = "Claim successfully passed all validation rules"
        elif val_output.overall_result == ValidationOutcomeEnum.BLOCKED_DOCS:
            target_status = ClaimStatus.DOCUMENTS_PENDING
            reason_msg = (
                f"Validation blocked by missing/unverified documents: "
                f"{', '.join(val_output.blocked_document_reasons)}"
            )
        else:
            target_status = ClaimStatus.VALIDATION_FAILED
            failing_codes = [
                r.reason_code
                for r in val_output.rule_results
                if r.outcome.value == "FAIL" and r.reason_code
            ]
            reason_msg = f"Validation failed on rules: {', '.join(failing_codes)}"

        # Validate transition from REGISTERED to target_status
        validate_transition(claim.status, target_status)

        old_status = claim.status
        claim.status = target_status
        claim.status_version += 1

        # Record status history
        history = ClaimStatusHistory(
            claim_id=claim.id,
            from_status=old_status.value,
            to_status=target_status.value,
            changed_by=actor.id,
            reason=reason_msg,
            created_at=utc_now(),
        )
        self.db.add(history)

        # Write audit log entries
        write_audit_log(
            db=self.db,
            action="CLAIM_VALIDATED",
            entity_type="ValidationRun",
            entity_id=str(run_id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim.id,
            event_data={
                "ruleset_version": val_output.ruleset_version,
                "overall_result": val_output.overall_result.value,
                "evaluated_on": val_output.evaluated_on.isoformat(),
                "failing_rules_count": sum(
                    1 for r in val_output.rule_results if r.outcome.value == "FAIL"
                ),
            },
        )
        write_audit_log(
            db=self.db,
            action="CLAIM_STATUS_TRANSITION",
            entity_type="Claim",
            entity_id=str(claim.id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim.id,
            event_data={
                "from_status": old_status.value,
                "to_status": target_status.value,
                "status_version": claim.status_version,
                "reason_sha256": hash_free_text(reason_msg),
            },
        )

        self.db.commit()
        self.db.refresh(validation_run)
        return validation_run

    def get_validation_runs(self, claim_id: uuid.UUID, actor: User) -> list[ValidationRun]:
        """Retrieve all validation runs for a claim with authorization check."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise ValidationServiceError(f"Claim '{claim_id}' not found")
        check_claim_access(actor, claim)

        stmt = (
            select(ValidationRun)
            .where(ValidationRun.claim_id == claim_id)
            .order_by(ValidationRun.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())

    def get_latest_validation_run(self, claim_id: uuid.UUID, actor: User) -> ValidationRun | None:
        """Retrieve the latest validation run for a claim."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise ValidationServiceError(f"Claim '{claim_id}' not found")
        check_claim_access(actor, claim)
        runs = self.get_validation_runs(claim_id, actor)
        return runs[0] if runs else None

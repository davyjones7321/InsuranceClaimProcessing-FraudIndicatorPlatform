"""Domain orchestration service for claim risk evaluation against active ruleset."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
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
    Customer,
    Decision,
    HistoryCoverage,
    Investigation,
    Policy,
    RequiredDocumentType,
    RiskEvaluation,
    RiskIndicatorResult,
    User,
)
from app.domain.enums import (
    ClaimStatus,
    DecisionType,
    EvidenceLevel,
    IndicatorEvaluationStatus,
    InvestigationStatus,
    RiskCategory,
    RiskCompleteness,
    UserRole,
)
from app.domain.state_machine import FINAL_STATES, validate_transition
from app.engine.risk import evaluate_risk
from app.engine.types import (
    ClaimInput,
    CoverageInput,
    DocumentInput,
    HistoricalClaimInput,
    PolicyInput,
    RiskCompletenessEnum,
    RiskInput,
)
from app.rules.loader import ACTIVE_RULESET
from app.schemas.snapshots import compute_ruleset_hash, serialize_risk_snapshot
from app.security.rbac import PermissionDeniedError, check_claim_access, check_risk_access


class RiskServiceError(ValueError):
    """Raised when risk evaluation preconditions or state constraints fail."""

    pass


class RiskService:
    """Orchestration service connecting database state to pure deterministic risk engine."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def build_risk_input(
        self,
        claim: Claim,
        evaluation_date: date | None = None,
        ruleset: dict[str, Any] | None = None,
    ) -> RiskInput:
        """Construct pure RiskInput dataclass from database state without protected attributes."""
        customer = self.db.get(Customer, claim.customer_id)
        policy = self.db.get(Policy, claim.policy_id)

        # Matching coverage for claim category
        coverage = self.db.scalars(
            select(Coverage).where(
                Coverage.policy_id == claim.policy_id,
                Coverage.claim_category == claim.claim_category,
            )
        ).first()

        # Valid documents for inconsistency verification
        documents = list(
            self.db.scalars(select(ClaimDocument).where(ClaimDocument.claim_id == claim.id)).all()
        )

        # Historical claims for repeated-claim & duplicate indicators (excluding WITHDRAWN)
        hist_stmt = select(Claim).where(
            or_(
                Claim.customer_id == claim.customer_id,
                Claim.policy_id == claim.policy_id,
            ),
            Claim.id != claim.id,
            Claim.status != ClaimStatus.WITHDRAWN,
        )
        hist_claims = list(self.db.scalars(hist_stmt).all())

        # Peer approved amounts for same product type and claim category (R04)
        peer_amounts: tuple[Decimal, ...] = ()
        if policy:
            peer_stmt = (
                select(Decision.approved_amount)
                .join(Claim, Decision.claim_id == Claim.id)
                .join(Policy, Claim.policy_id == Policy.id)
                .where(
                    Policy.product_type == policy.product_type,
                    Claim.claim_category == claim.claim_category,
                    Decision.decision == DecisionType.APPROVED,
                    Decision.approved_amount.is_not(None),
                )
            )
            peer_amounts = tuple(amt for amt in self.db.scalars(peer_stmt).all() if amt is not None)

        # History coverage records
        hist_cov_stmt = select(HistoryCoverage)
        hist_cov_records = list(self.db.scalars(hist_cov_stmt).all())
        history_cov_map = {hc.product_type: hc.loaded_from for hc in hist_cov_records}

        # Evaluation date in configured business timezone
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
            incident_group_id=str(claim.incident_group_id) if claim.incident_group_id else None,
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

        coverage_input: CoverageInput | None = None
        if coverage:
            coverage_input = CoverageInput(
                coverage_id=str(coverage.id),
                claim_category=coverage.claim_category,
                limit_amount=coverage.limit_amount,
                consumed_amount=coverage.consumed_amount,
                deductible=coverage.deductible,
                copay_percent=coverage.copay_percent,
                waiting_period_days=coverage.waiting_period_days,
                is_excluded=coverage.is_excluded,
            )

        # Required document types for product type to determine identity-bearing docs
        if policy:
            req_stmt = select(RequiredDocumentType).where(
                RequiredDocumentType.product_type == policy.product_type
            )
            if claim.claim_category:
                req_stmt = req_stmt.where(
                    (RequiredDocumentType.claim_category == claim.claim_category)
                    | (RequiredDocumentType.claim_category.is_(None))
                )
            req_doc_map = {r.doc_code: r for r in self.db.scalars(req_stmt).all()}
        else:
            req_doc_map = {}

        current_identity_version = customer.identity_version if customer else 1

        doc_inputs_list: list[DocumentInput] = []
        for d in documents:
            req_type = req_doc_map.get(d.doc_code)
            is_id_bearing = req_type.is_identity_bearing if req_type else False

            latest_att = max(d.attestations, key=lambda a: a.rev) if d.attestations else None
            is_att_current = (
                latest_att.subject_identity_version == current_identity_version
                if latest_att
                else False
            )
            att_result = latest_att.result.value if latest_att else None

            doc_inputs_list.append(
                DocumentInput(
                    document_id=str(d.id),
                    doc_code=d.doc_code,
                    status=d.status.value,
                    named_person=d.named_person,
                    document_date=d.document_date,
                    is_identity_bearing=is_id_bearing,
                    is_identity_attestation_current=is_att_current,
                    identity_attestation_result=att_result,
                )
            )

        document_inputs = tuple(doc_inputs_list)

        historical_inputs = tuple(
            HistoricalClaimInput(
                claim_id=str(hc.id),
                claim_ref=hc.claim_ref,
                customer_id=str(hc.customer_id),
                policy_id=str(hc.policy_id),
                claim_category=hc.claim_category,
                incident_date=hc.incident_date,
                registered_date=hc.registered_at.date(),
                requested_amount=hc.requested_amount,
                status=hc.status.value,
                incident_group_id=str(hc.incident_group_id) if hc.incident_group_id else None,
            )
            for hc in hist_claims
        )

        return RiskInput(
            claim=claim_input,
            policy=policy_input,
            coverage=coverage_input,
            documents=document_inputs,
            historical_claims=historical_inputs,
            peer_approved_amounts=peer_amounts,
            evaluation_date=evaluation_date,
            ruleset=active_rules,
            history_coverage=history_cov_map,
        )

    def evaluate_claim_risk(
        self,
        claim_id: uuid.UUID,
        actor: User,
        evaluation_date: date | None = None,
        ruleset: dict[str, Any] | None = None,
    ) -> RiskEvaluation:
        """Run pure risk indicators, persist evaluation, and transition claim status."""
        stmt = select(Claim).where(Claim.id == claim_id)
        bind = self.db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()

        claim = self.db.scalars(stmt).first()
        if not claim:
            raise RiskServiceError(f"Claim '{claim_id}' not found")

        # Object-level access verification
        check_claim_access(actor, claim, action="MUTATE")
        if actor.role in (UserRole.CUSTOMER, UserRole.STAFF):
            raise PermissionDeniedError(
                f"Role '{actor.role.value}' is not authorized to evaluate claim risk"
            )

        # Section 10: Runs only after validation PASS (claim must be VALIDATED or re-evaluating in later states)
        allowed_eval_statuses = (
            ClaimStatus.VALIDATED,
            ClaimStatus.RISK_EVALUATED,
            ClaimStatus.RISK_DATA_PENDING,
            ClaimStatus.UNDER_INVESTIGATION,
            ClaimStatus.READY_FOR_ASSESSMENT,
            ClaimStatus.ASSESSED,
        )
        if claim.status not in allowed_eval_statuses:
            raise RiskServiceError(
                f"Cannot evaluate risk for claim in status '{claim.status.value}'. Must be VALIDATED first."
            )

        # Construct pure input and run evaluation
        risk_input = self.build_risk_input(
            claim=claim,
            evaluation_date=evaluation_date,
            ruleset=ruleset,
        )
        risk_output = evaluate_risk(risk_input)

        active_rules = ruleset or ACTIVE_RULESET
        ruleset_hash = compute_ruleset_hash(active_rules)
        risk_snapshot = serialize_risk_snapshot(risk_input, ruleset_hash=ruleset_hash)

        # Persist RiskEvaluation row
        eval_id = uuid.uuid4()
        risk_evaluation = RiskEvaluation(
            id=eval_id,
            claim_id=claim.id,
            ruleset_version=risk_output.ruleset_version,
            engine_version=risk_output.engine_version,
            ruleset_hash=ruleset_hash,
            evaluated_on=risk_output.evaluated_on,
            score=risk_output.score,
            category=RiskCategory(risk_output.category.value),
            completeness=RiskCompleteness(risk_output.completeness.value),
            composed_label=risk_output.composed_label,
            requires_investigation=risk_output.requires_investigation,
            routing_reasons=list(risk_output.routing_reasons),
            pilot_banner=risk_output.pilot_banner,
            group_scores=risk_output.group_scores,
            inputs_snapshot=risk_snapshot,
            calculation_trace={
                "score": risk_output.score,
                "category": risk_output.category.value,
                "completeness": risk_output.completeness.value,
                "requires_investigation": risk_output.requires_investigation,
                "group_scores": risk_output.group_scores,
                "routing_reasons": list(risk_output.routing_reasons),
            },
            created_at=utc_now(),
        )
        self.db.add(risk_evaluation)

        # Persist indicator results
        for ind in risk_output.indicator_results:
            ind_row = RiskIndicatorResult(
                id=uuid.uuid4(),
                evaluation_id=eval_id,
                indicator_id=ind.indicator_id,
                indicator_version=ind.indicator_version,
                group=ind.group,
                weight=ind.weight,
                triggered=ind.triggered,
                weight_applied=ind.weight_applied,
                sufficient_alone=ind.sufficient_alone,
                evidence_level=EvidenceLevel(ind.evidence_level),
                skip_reason=ind.skip_reason,
                evidence=ind.evidence,
                message=ind.message,
                status=IndicatorEvaluationStatus(ind.status),
                created_at=utc_now(),
            )
            self.db.add(ind_row)

        # If claim is in VALIDATED or RISK_DATA_PENDING status, transition through state machine
        if claim.status in (ClaimStatus.VALIDATED, ClaimStatus.RISK_DATA_PENDING):
            initial_status = claim.status
            # Step 1: transition to RISK_EVALUATED (or re-evaluating)
            if claim.status == ClaimStatus.RISK_DATA_PENDING:
                validate_transition(claim.status, ClaimStatus.VALIDATED)
                claim.status = ClaimStatus.VALIDATED
                claim.status_version += 1

            validate_transition(claim.status, ClaimStatus.RISK_EVALUATED)
            claim.status = ClaimStatus.RISK_EVALUATED
            claim.status_version += 1
            hist_step1 = ClaimStatusHistory(
                claim_id=claim.id,
                from_status=initial_status.value,
                to_status=ClaimStatus.RISK_EVALUATED.value,
                changed_by=actor.id,
                reason=f"Risk evaluation complete: score={risk_output.score}, composed_label={risk_output.composed_label}",
                created_at=utc_now(),
            )
            self.db.add(hist_step1)

            # Step 2: Route to RISK_DATA_PENDING, UNDER_INVESTIGATION, or READY_FOR_ASSESSMENT
            if risk_output.completeness == RiskCompletenessEnum.INSUFFICIENT_DATA:
                target_status = ClaimStatus.RISK_DATA_PENDING
                transition_reason = f"Insufficient risk data: transitioning to RISK_DATA_PENDING ({risk_output.composed_label})"
            elif risk_output.requires_investigation:
                target_status = ClaimStatus.UNDER_INVESTIGATION
                transition_reason = (
                    f"Flagged for investigation: {', '.join(risk_output.routing_reasons)}"
                )

                # Find prior investigation if any
                prior_inv = self.db.scalars(
                    select(Investigation)
                    .where(Investigation.claim_id == claim.id)
                    .order_by(Investigation.opened_at.desc())
                ).first()

                investigation = Investigation(
                    id=uuid.uuid4(),
                    claim_id=claim.id,
                    previous_investigation_id=prior_inv.id if prior_inv else None,
                    assigned_to=None,
                    assigned_by=actor.id,
                    status=InvestigationStatus.ASSIGNED,
                    referral_type="AUTOMATED",
                    opened_at=utc_now(),
                    created_at=utc_now(),
                )
                self.db.add(investigation)
                write_audit_log(
                    db=self.db,
                    action="INVESTIGATION_CREATED",
                    entity_type="Investigation",
                    entity_id=str(investigation.id),
                    actor_role=actor.role.value,
                    actor_id=actor.id,
                    claim_id=claim.id,
                    event_data={
                        "score": risk_output.score,
                        "category": risk_output.category.value,
                        "status": investigation.status.value,
                        "referral_type": "AUTOMATED",
                    },
                )
            else:
                target_status = ClaimStatus.READY_FOR_ASSESSMENT
                transition_reason = f"Low risk score: routed directly to assessment queue ({risk_output.composed_label})"

            validate_transition(claim.status, target_status)
            claim.status = target_status
            claim.status_version += 1

            hist_step2 = ClaimStatusHistory(
                claim_id=claim.id,
                from_status=ClaimStatus.RISK_EVALUATED.value,
                to_status=target_status.value,
                changed_by=actor.id,
                reason=transition_reason,
                created_at=utc_now(),
            )
            self.db.add(hist_step2)

            write_audit_log(
                db=self.db,
                action="CLAIM_STATUS_TRANSITION",
                entity_type="Claim",
                entity_id=str(claim.id),
                actor_role=actor.role.value,
                actor_id=actor.id,
                claim_id=claim.id,
                event_data={
                    "from_status": ClaimStatus.RISK_EVALUATED.value,
                    "to_status": target_status.value,
                    "status_version": claim.status_version,
                    "reason_sha256": hash_free_text(transition_reason),
                },
            )

        # Audit write for risk evaluation
        write_audit_log(
            db=self.db,
            action="CLAIM_RISK_EVALUATED",
            entity_type="RiskEvaluation",
            entity_id=str(eval_id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim.id,
            event_data={
                "score": risk_output.score,
                "category": risk_output.category.value,
                "completeness": risk_output.completeness.value,
                "composed_label": risk_output.composed_label,
                "requires_investigation": risk_output.requires_investigation,
                "triggered_count": sum(1 for r in risk_output.indicator_results if r.triggered),
            },
        )

        self.db.commit()
        self.db.refresh(risk_evaluation)
        return risk_evaluation

    def record_completeness_waiver(
        self,
        claim_id: uuid.UUID,
        actor: User,
        remarks: str,
    ) -> Claim:
        """Record manager waiver for missing risk data (P6 Task 5, Decision D4)."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise RiskServiceError(f"Claim '{claim_id}' not found")

        check_claim_access(actor, claim, action="MUTATE")
        if actor.role != UserRole.MANAGER:
            raise PermissionDeniedError("Only managers may record completeness waivers")

        if claim.status != ClaimStatus.RISK_DATA_PENDING:
            raise RiskServiceError(
                f"Cannot record completeness waiver for claim in status '{claim.status.value}'. Must be RISK_DATA_PENDING."
            )

        clean_remarks = remarks.strip()
        if len(clean_remarks) < 20:
            raise RiskServiceError(
                "Completeness waiver remarks must be at least 20 characters long"
            )

        target_status = ClaimStatus.READY_FOR_ASSESSMENT
        validate_transition(claim.status, target_status)
        from_status_val = claim.status.value
        claim.status = target_status
        claim.status_version += 1

        hist = ClaimStatusHistory(
            claim_id=claim.id,
            from_status=from_status_val,
            to_status=target_status.value,
            changed_by=actor.id,
            reason=f"Manager completeness waiver: {clean_remarks}",
            created_at=utc_now(),
        )
        self.db.add(hist)

        write_audit_log(
            db=self.db,
            action="RISK_COMPLETENESS_WAIVER",
            entity_type="Claim",
            entity_id=str(claim.id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim.id,
            event_data={
                "from_status": from_status_val,
                "to_status": target_status.value,
                "status_version": claim.status_version,
                "remarks_sha256": hash_free_text(clean_remarks),
            },
        )
        self.db.commit()
        self.db.refresh(claim)
        return claim

    def refer_to_investigation(
        self,
        claim_id: uuid.UUID,
        actor: User,
        remarks: str,
    ) -> Investigation:
        """Refer any open claim to investigation with mandatory remarks (P6 Task 10)."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise RiskServiceError(f"Claim '{claim_id}' not found")

        check_claim_access(actor, claim, action="MUTATE")
        if actor.role not in (UserRole.ASSESSOR, UserRole.MANAGER):
            raise PermissionDeniedError(
                f"Role '{actor.role.value}' is not authorized to refer claims to investigation"
            )

        if claim.status in FINAL_STATES:
            raise RiskServiceError(
                f"Cannot refer claim in final status '{claim.status.value}' to investigation"
            )

        clean_remarks = remarks.strip()
        if len(clean_remarks) < 20:
            raise RiskServiceError("Manual referral remarks must be at least 20 characters long")

        target_status = ClaimStatus.UNDER_INVESTIGATION
        validate_transition(claim.status, target_status)
        from_status_val = claim.status.value
        claim.status = target_status
        claim.status_version += 1

        hist = ClaimStatusHistory(
            claim_id=claim.id,
            from_status=from_status_val,
            to_status=target_status.value,
            changed_by=actor.id,
            reason=f"Manual investigation referral: {clean_remarks}",
            created_at=utc_now(),
        )
        self.db.add(hist)

        prior_inv = self.db.scalars(
            select(Investigation)
            .where(Investigation.claim_id == claim.id)
            .order_by(Investigation.opened_at.desc())
        ).first()

        inv_id = uuid.uuid4()
        investigation = Investigation(
            id=inv_id,
            claim_id=claim.id,
            previous_investigation_id=prior_inv.id if prior_inv else None,
            assigned_to=None,
            assigned_by=actor.id,
            status=InvestigationStatus.ASSIGNED,
            referral_type="MANUAL_REFERRAL",
            referral_remarks=clean_remarks,
            opened_at=utc_now(),
            created_at=utc_now(),
        )
        self.db.add(investigation)

        write_audit_log(
            db=self.db,
            action="MANUAL_INVESTIGATION_REFERRAL",
            entity_type="Investigation",
            entity_id=str(inv_id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=claim.id,
            event_data={
                "from_status": from_status_val,
                "to_status": target_status.value,
                "status_version": claim.status_version,
                "remarks_sha256": hash_free_text(clean_remarks),
            },
        )
        self.db.commit()
        self.db.refresh(investigation)
        return investigation

    def record_investigation_feedback(
        self,
        investigation_id: uuid.UUID,
        actor: User,
        indicator_feedback: dict[str, Any],
    ) -> Investigation:
        """Record investigator structured feedback on triggered indicators at closure (P6 Task 13)."""
        inv = self.db.get(Investigation, investigation_id)
        if not inv:
            raise RiskServiceError(f"Investigation '{investigation_id}' not found")

        if actor.role == UserRole.INVESTIGATOR and inv.assigned_to != actor.id:
            raise PermissionDeniedError(
                "Investigators may only record feedback on their assigned investigations"
            )
        if actor.role not in (UserRole.INVESTIGATOR, UserRole.MANAGER):
            raise PermissionDeniedError("Only investigators or managers may record feedback")

        clean_feedback: dict[str, Any] = {}
        for k, v in indicator_feedback.items():
            if hasattr(v, "model_dump"):
                clean_feedback[k] = v.model_dump()
            elif isinstance(v, dict):
                clean_feedback[k] = v
            else:
                clean_feedback[k] = {"status": str(v), "notes": None}

        inv.indicator_feedback = clean_feedback
        audit_feedback = {
            k: {
                "status": v["status"] if isinstance(v, dict) else str(v),
                "notes_sha256": hash_free_text(v.get("notes"))
                if isinstance(v, dict) and v.get("notes")
                else None,
            }
            for k, v in clean_feedback.items()
        }
        write_audit_log(
            db=self.db,
            action="INVESTIGATION_FEEDBACK_RECORDED",
            entity_type="Investigation",
            entity_id=str(inv.id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=inv.claim_id,
            event_data={"feedback": audit_feedback},
        )
        self.db.commit()
        self.db.refresh(inv)
        return inv

    def get_risk_evaluations(self, claim_id: uuid.UUID, actor: User) -> list[RiskEvaluation]:
        """Retrieve all risk evaluations for a claim with authorization verification."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise RiskServiceError(f"Claim '{claim_id}' not found")
        check_risk_access(actor, claim)

        stmt = (
            select(RiskEvaluation)
            .where(RiskEvaluation.claim_id == claim_id)
            .order_by(RiskEvaluation.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())

    def get_latest_risk_evaluation(self, claim_id: uuid.UUID, actor: User) -> RiskEvaluation | None:
        """Retrieve the most recent risk evaluation for a claim."""
        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise RiskServiceError(f"Claim '{claim_id}' not found")
        check_risk_access(actor, claim)
        runs = self.get_risk_evaluations(claim_id, actor)
        return runs[0] if runs else None

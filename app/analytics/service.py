"""Analytics service computing SQL aggregates for the manager dashboard and CSV export."""

import csv
import io
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.models import (
    Claim,
    Customer,
    Decision,
    Investigation,
    Policy,
    RiskEvaluation,
    RiskIndicatorResult,
    ValidationResult,
    ValidationRun,
)
from app.domain.enums import (
    ClaimStatus,
    DecisionType,
    InvestigationStatus,
    ProductType,
    RuleOutcome,
)
from app.schemas.analytics import (
    AnalyticsDashboardResponse,
    BacklogMetrics,
    FinancialMetrics,
    InvestigationMetrics,
    ProcessingTimeMetrics,
    ProductFinancialSummary,
    ReasonCodeCount,
    RiskMetrics,
    VolumeByProduct,
    VolumeByStatus,
    VolumeOverTime,
)


class AnalyticsService:
    """Service executing high-performance SQL aggregate queries for managerial reporting."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def _apply_claim_filters(
        self,
        stmt: Any,
        start_date: date | None = None,
        end_date: date | None = None,
        product_type: ProductType | None = None,
        claim_category: str | None = None,
        status: ClaimStatus | None = None,
    ) -> Any:
        """Apply standard managerial query filters to a claim-based select statement."""
        if start_date:
            start_dt = datetime.combine(start_date, time.min).replace(tzinfo=UTC)
            stmt = stmt.where(Claim.registered_at >= start_dt)
        if end_date:
            end_dt = datetime.combine(end_date, time.max).replace(tzinfo=UTC)
            stmt = stmt.where(Claim.registered_at <= end_dt)
        if product_type:
            stmt = stmt.where(Policy.product_type == product_type)
        if claim_category:
            stmt = stmt.where(Claim.claim_category == claim_category)
        if status:
            stmt = stmt.where(Claim.status == status)
        return stmt

    def get_dashboard_metrics(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        product_type: ProductType | None = None,
        claim_category: str | None = None,
        status: ClaimStatus | None = None,
    ) -> AnalyticsDashboardResponse:
        """Compute all dashboard operational and risk metrics via pure SQL aggregates."""
        # 1. Volume by Product
        stmt_prod = (
            select(Policy.product_type, func.count(Claim.id))
            .join(Policy, Claim.policy_id == Policy.id)
            .group_by(Policy.product_type)
        )
        stmt_prod = self._apply_claim_filters(
            stmt_prod, start_date, end_date, product_type, claim_category, status
        )
        prod_rows = self.db.execute(stmt_prod).all()
        volume_by_product = [
            VolumeByProduct(product_type=p.value if hasattr(p, "value") else str(p), count=c)
            for p, c in prod_rows
        ]

        # 2. Volume by Status
        stmt_status = (
            select(Claim.status, func.count(Claim.id))
            .join(Policy, Claim.policy_id == Policy.id)
            .group_by(Claim.status)
        )
        stmt_status = self._apply_claim_filters(
            stmt_status, start_date, end_date, product_type, claim_category, status
        )
        status_rows = self.db.execute(stmt_status).all()
        volume_by_status = [
            VolumeByStatus(status=s.value if hasattr(s, "value") else str(s), count=c)
            for s, c in status_rows
        ]

        # 3. Volume Over Time (by registration day)
        date_expr = func.date(Claim.registered_at)
        stmt_time = (
            select(date_expr, func.count(Claim.id))
            .join(Policy, Claim.policy_id == Policy.id)
            .group_by(date_expr)
            .order_by(date_expr.asc())
        )
        stmt_time = self._apply_claim_filters(
            stmt_time, start_date, end_date, product_type, claim_category, status
        )
        time_rows = self.db.execute(stmt_time).all()
        volume_over_time = [
            VolumeOverTime(period=str(d), count=c)
            for d, c in time_rows
        ]

        # 4. Processing Time Metrics (registered_at -> decision.created_at)
        proc_stmt = (
            select(
                Claim.id,
                Policy.product_type,
                Claim.registered_at,
                func.min(Decision.created_at).label("decided_at"),
            )
            .join(Policy, Claim.policy_id == Policy.id)
            .join(Decision, Claim.id == Decision.claim_id)
            .group_by(Claim.id, Policy.product_type, Claim.registered_at)
        )
        proc_stmt = self._apply_claim_filters(
            proc_stmt, start_date, end_date, product_type, claim_category, status
        )
        proc_rows = self.db.execute(proc_stmt).all()

        durations_by_product: dict[str, list[float]] = {}
        all_durations: list[float] = []

        for _, p_type, reg_at, dec_at in proc_rows:
            if reg_at and dec_at:
                if reg_at.tzinfo is None:
                    reg_at = reg_at.replace(tzinfo=UTC)
                if dec_at.tzinfo is None:
                    dec_at = dec_at.replace(tzinfo=UTC)
                diff_hours = (dec_at - reg_at).total_seconds() / 3600.0
                all_durations.append(diff_hours)
                p_key = p_type.value if hasattr(p_type, "value") else str(p_type)
                durations_by_product.setdefault(p_key, []).append(diff_hours)

        if all_durations:
            all_durations.sort()
            avg_hours = round(sum(all_durations) / len(all_durations), 2)
            n = len(all_durations)
            if n % 2 == 1:
                median_hours = round(all_durations[n // 2], 2)
            else:
                median_hours = round((all_durations[n // 2 - 1] + all_durations[n // 2]) / 2.0, 2)
        else:
            avg_hours = 0.0
            median_hours = 0.0

        prod_avgs: dict[str, float] = {}
        for p_k, dur_list in durations_by_product.items():
            if dur_list:
                prod_avgs[p_k] = round(sum(dur_list) / len(dur_list), 2)

        processing_time = ProcessingTimeMetrics(
            overall_avg_hours=avg_hours,
            overall_median_hours=median_hours,
            by_product=prod_avgs,
        )

        # 5. Rejection Reasons (from Decision records)
        rej_decisions_stmt = (
            select(Decision.reason_codes)
            .join(Claim, Decision.claim_id == Claim.id)
            .join(Policy, Claim.policy_id == Policy.id)
            .where(Decision.decision == DecisionType.REJECTED)
        )
        rej_decisions_stmt = self._apply_claim_filters(
            rej_decisions_stmt, start_date, end_date, product_type, claim_category, status
        )
        rej_rows = self.db.execute(rej_decisions_stmt).all()
        rejection_counts: dict[str, int] = {}
        for (rc_list,) in rej_rows:
            if isinstance(rc_list, list):
                for code in rc_list:
                    rejection_counts[code] = rejection_counts.get(code, 0) + 1
        rejection_reasons = [
            ReasonCodeCount(reason_code=code, count=cnt)
            for code, cnt in sorted(rejection_counts.items(), key=lambda x: x[1], reverse=True)
        ]

        # 6. Validation Failure Reasons (from ValidationResult records)
        val_fail_stmt = (
            select(ValidationResult.reason_code, func.count(ValidationResult.id))
            .join(ValidationRun, ValidationResult.run_id == ValidationRun.id)
            .join(Claim, ValidationRun.claim_id == Claim.id)
            .join(Policy, Claim.policy_id == Policy.id)
            .where(
                ValidationResult.outcome == RuleOutcome.FAIL,
                ValidationResult.reason_code.isnot(None),
            )
            .group_by(ValidationResult.reason_code)
            .order_by(func.count(ValidationResult.id).desc())
        )
        val_fail_stmt = self._apply_claim_filters(
            val_fail_stmt, start_date, end_date, product_type, claim_category, status
        )
        val_fail_rows = self.db.execute(val_fail_stmt).all()
        validation_failure_reasons = [
            ReasonCodeCount(reason_code=str(rc), count=c)
            for rc, c in val_fail_rows
        ]

        # 7. Risk Metrics (from RiskEvaluation and RiskIndicatorResult)
        risk_eval_stmt = (
            select(
                func.count(RiskEvaluation.id).label("total_evals"),
                func.sum(
                    case((RiskEvaluation.requires_investigation.is_(True), 1), else_=0)
                ).label("flagged_evals"),
            )
            .join(Claim, RiskEvaluation.claim_id == Claim.id)
            .join(Policy, Claim.policy_id == Policy.id)
        )
        risk_eval_stmt = self._apply_claim_filters(
            risk_eval_stmt, start_date, end_date, product_type, claim_category, status
        )
        total_evals_res, flagged_evals_res = self.db.execute(risk_eval_stmt).one()
        total_evals = total_evals_res or 0
        flagged_evals = flagged_evals_res or 0
        inv_rate = round((flagged_evals / total_evals * 100.0) if total_evals > 0 else 0.0, 2)

        cat_dist_stmt = (
            select(RiskEvaluation.category, func.count(RiskEvaluation.id))
            .join(Claim, RiskEvaluation.claim_id == Claim.id)
            .join(Policy, Claim.policy_id == Policy.id)
            .group_by(RiskEvaluation.category)
        )
        cat_dist_stmt = self._apply_claim_filters(
            cat_dist_stmt, start_date, end_date, product_type, claim_category, status
        )
        cat_dist_rows = self.db.execute(cat_dist_stmt).all()
        cat_dist = {
            c.value if hasattr(c, "value") else str(c): cnt
            for c, cnt in cat_dist_rows
        }

        ind_freq_stmt = (
            select(RiskIndicatorResult.indicator_id, func.count(RiskIndicatorResult.id))
            .join(RiskEvaluation, RiskIndicatorResult.evaluation_id == RiskEvaluation.id)
            .join(Claim, RiskEvaluation.claim_id == Claim.id)
            .join(Policy, Claim.policy_id == Policy.id)
            .where(RiskIndicatorResult.triggered.is_(True))
            .group_by(RiskIndicatorResult.indicator_id)
            .order_by(func.count(RiskIndicatorResult.id).desc())
        )
        ind_freq_stmt = self._apply_claim_filters(
            ind_freq_stmt, start_date, end_date, product_type, claim_category, status
        )
        ind_freq_rows = self.db.execute(ind_freq_stmt).all()
        ind_freqs = {str(i_id): cnt for i_id, cnt in ind_freq_rows}

        risk_metrics = RiskMetrics(
            total_evaluations=total_evals,
            flagged_evaluations=flagged_evals,
            investigation_rate_percent=inv_rate,
            category_distribution=cat_dist,
            indicator_frequencies=ind_freqs,
        )

        # 8. Investigation Metrics
        inv_stmt = (
            select(
                func.sum(
                    case((Investigation.status != InvestigationStatus.COMPLETED, 1), else_=0)
                ).label("open_count"),
                func.sum(
                    case((Investigation.status == InvestigationStatus.ESCALATED, 1), else_=0)
                ).label("escalated_count"),
                func.sum(
                    case((Investigation.status == InvestigationStatus.COMPLETED, 1), else_=0)
                ).label("completed_count"),
            )
            .join(Claim, Investigation.claim_id == Claim.id)
            .join(Policy, Claim.policy_id == Policy.id)
        )
        inv_stmt = self._apply_claim_filters(
            inv_stmt, start_date, end_date, product_type, claim_category, status
        )
        open_cnt, esc_cnt, comp_cnt = self.db.execute(inv_stmt).one()

        inv_outcomes_stmt = (
            select(Investigation.outcome, func.count(Investigation.id))
            .join(Claim, Investigation.claim_id == Claim.id)
            .join(Policy, Claim.policy_id == Policy.id)
            .where(Investigation.outcome.isnot(None))
            .group_by(Investigation.outcome)
        )
        inv_outcomes_stmt = self._apply_claim_filters(
            inv_outcomes_stmt, start_date, end_date, product_type, claim_category, status
        )
        outcome_rows = self.db.execute(inv_outcomes_stmt).all()
        outcomes_dist = {
            o.value if hasattr(o, "value") else str(o): cnt
            for o, cnt in outcome_rows
        }

        # Investigation durations
        inv_dur_stmt = (
            select(Investigation.opened_at, Investigation.closed_at)
            .join(Claim, Investigation.claim_id == Claim.id)
            .join(Policy, Claim.policy_id == Policy.id)
            .where(
                Investigation.status == InvestigationStatus.COMPLETED,
                Investigation.closed_at.isnot(None),
            )
        )
        inv_dur_stmt = self._apply_claim_filters(
            inv_dur_stmt, start_date, end_date, product_type, claim_category, status
        )
        inv_dur_rows = self.db.execute(inv_dur_stmt).all()
        dur_hours = []
        for o_at, c_at in inv_dur_rows:
            if o_at and c_at:
                if o_at.tzinfo is None:
                    o_at = o_at.replace(tzinfo=UTC)
                if c_at.tzinfo is None:
                    c_at = c_at.replace(tzinfo=UTC)
                dur_hours.append((c_at - o_at).total_seconds() / 3600.0)
        avg_inv_duration = round(sum(dur_hours) / len(dur_hours), 2) if dur_hours else 0.0

        investigation_metrics = InvestigationMetrics(
            open_count=open_cnt or 0,
            escalated_count=esc_cnt or 0,
            completed_count=comp_cnt or 0,
            avg_duration_hours=avg_inv_duration,
            outcome_distribution=outcomes_dist,
        )

        # 9. Financial Metrics (Requested vs Approved)
        fin_stmt = (
            select(
                func.coalesce(func.sum(Claim.requested_amount), Decimal("0.00")),
            )
            .join(Policy, Claim.policy_id == Policy.id)
        )
        fin_stmt = self._apply_claim_filters(
            fin_stmt, start_date, end_date, product_type, claim_category, status
        )
        total_req = self.db.scalar(fin_stmt) or Decimal("0.00")

        # Approved amount from Decision records
        app_stmt = (
            select(
                func.coalesce(func.sum(Decision.approved_amount), Decimal("0.00")),
            )
            .join(Claim, Decision.claim_id == Claim.id)
            .join(Policy, Claim.policy_id == Policy.id)
            .where(Decision.decision == DecisionType.APPROVED)
        )
        app_stmt = self._apply_claim_filters(
            app_stmt, start_date, end_date, product_type, claim_category, status
        )
        total_app = self.db.scalar(app_stmt) or Decimal("0.00")

        # Per product requested and approved
        prod_req_stmt = (
            select(Policy.product_type, func.coalesce(func.sum(Claim.requested_amount), Decimal("0.00")))
            .join(Policy, Claim.policy_id == Policy.id)
            .group_by(Policy.product_type)
        )
        prod_req_stmt = self._apply_claim_filters(
            prod_req_stmt, start_date, end_date, product_type, claim_category, status
        )
        prod_req_rows = self.db.execute(prod_req_stmt).all()

        prod_app_stmt = (
            select(Policy.product_type, func.coalesce(func.sum(Decision.approved_amount), Decimal("0.00")))
            .join(Claim, Decision.claim_id == Claim.id)
            .join(Policy, Claim.policy_id == Policy.id)
            .where(Decision.decision == DecisionType.APPROVED)
            .group_by(Policy.product_type)
        )
        prod_app_stmt = self._apply_claim_filters(
            prod_app_stmt, start_date, end_date, product_type, claim_category, status
        )
        prod_app_rows = dict(self.db.execute(prod_app_stmt).all())

        by_product_fin: dict[str, ProductFinancialSummary] = {}
        for p_t, req_val in prod_req_rows:
            p_key = p_t.value if hasattr(p_t, "value") else str(p_t)
            app_val = prod_app_rows.get(p_t, Decimal("0.00"))
            by_product_fin[p_key] = ProductFinancialSummary(requested=req_val, approved=app_val)

        financial_metrics = FinancialMetrics(
            total_requested=total_req,
            total_approved=total_app,
            by_product=by_product_fin,
        )

        # 10. Backlog by Status and Age
        final_statuses = (ClaimStatus.APPROVED, ClaimStatus.REJECTED, ClaimStatus.WITHDRAWN)
        backlog_stmt = (
            select(Claim.status, func.count(Claim.id))
            .join(Policy, Claim.policy_id == Policy.id)
            .where(Claim.status.notin_(final_statuses))
            .group_by(Claim.status)
        )
        backlog_stmt = self._apply_claim_filters(
            backlog_stmt, start_date, end_date, product_type, claim_category, status
        )
        backlog_rows = self.db.execute(backlog_stmt).all()
        by_status_backlog = {
            s.value if hasattr(s, "value") else str(s): cnt
            for s, cnt in backlog_rows
        }
        total_backlog = sum(by_status_backlog.values())

        # Age buckets (< 7 days, 7-30 days, > 30 days)
        now_dt = utc_now()
        age_stmt = (
            select(Claim.registered_at)
            .join(Policy, Claim.policy_id == Policy.id)
            .where(Claim.status.notin_(final_statuses))
        )
        age_stmt = self._apply_claim_filters(
            age_stmt, start_date, end_date, product_type, claim_category, status
        )
        age_rows = self.db.scalars(age_stmt).all()
        age_buckets = {"<7d": 0, "7-30d": 0, ">30d": 0}
        for reg_dt in age_rows:
            if reg_dt.tzinfo is None:
                reg_dt = reg_dt.replace(tzinfo=UTC)
            days = (now_dt - reg_dt).total_seconds() / 86400.0
            if days < 7.0:
                age_buckets["<7d"] += 1
            elif days <= 30.0:
                age_buckets["7-30d"] += 1
            else:
                age_buckets[">30d"] += 1

        backlog_metrics = BacklogMetrics(
            total_active_backlog=total_backlog,
            by_status=by_status_backlog,
            by_age=age_buckets,
        )

        return AnalyticsDashboardResponse(
            volume_by_product=volume_by_product,
            volume_by_status=volume_by_status,
            volume_over_time=volume_over_time,
            processing_time=processing_time,
            rejection_reasons=rejection_reasons,
            validation_failure_reasons=validation_failure_reasons,
            risk_metrics=risk_metrics,
            investigation_metrics=investigation_metrics,
            financial_metrics=financial_metrics,
            backlog_metrics=backlog_metrics,
        )

    def export_claims_csv(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        product_type: ProductType | None = None,
        claim_category: str | None = None,
        status: ClaimStatus | None = None,
    ) -> str:
        """Generate formatted CSV string of claim records matching filtering criteria."""
        stmt = (
            select(
                Claim.claim_ref,
                Customer.customer_no,
                Customer.full_name,
                Policy.policy_no,
                Policy.product_type,
                Claim.claim_category,
                Claim.incident_date,
                Claim.registered_at,
                Claim.status,
                Claim.requested_amount,
                func.max(Decision.approved_amount).label("approved_amount"),
                func.max(Decision.created_at).label("decision_date"),
            )
            .join(Customer, Claim.customer_id == Customer.id)
            .join(Policy, Claim.policy_id == Policy.id)
            .outerjoin(Decision, Claim.id == Decision.claim_id)
            .group_by(
                Claim.claim_ref,
                Customer.customer_no,
                Customer.full_name,
                Policy.policy_no,
                Policy.product_type,
                Claim.claim_category,
                Claim.incident_date,
                Claim.registered_at,
                Claim.status,
                Claim.requested_amount,
            )
            .order_by(Claim.registered_at.desc())
        )
        stmt = self._apply_claim_filters(
            stmt, start_date, end_date, product_type, claim_category, status
        )
        rows = self.db.execute(stmt).all()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Claim Reference",
                "Customer Number",
                "Customer Name",
                "Policy Number",
                "Product Type",
                "Claim Category",
                "Incident Date",
                "Registration Timestamp",
                "Status",
                "Requested Amount",
                "Approved Amount",
                "Decision Timestamp",
            ]
        )

        for (
            c_ref,
            c_no,
            c_name,
            p_no,
            p_type,
            cat,
            inc_date,
            reg_at,
            stat,
            req_amt,
            app_amt,
            dec_at,
        ) in rows:
            writer.writerow(
                [
                    c_ref,
                    c_no,
                    c_name,
                    p_no,
                    p_type.value if hasattr(p_type, "value") else str(p_type),
                    cat,
                    str(inc_date),
                    reg_at.isoformat() if reg_at else "",
                    stat.value if hasattr(stat, "value") else str(stat),
                    f"{req_amt:.2f}" if req_amt is not None else "0.00",
                    f"{app_amt:.2f}" if app_amt is not None else "",
                    dec_at.isoformat() if dec_at else "",
                ]
            )

        return output.getvalue()

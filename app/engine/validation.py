"""Pure deterministic validation engine executing rules V01-V13."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.engine.types import (
    CoverageInput,
    DocumentInput,
    ExclusionInput,
    RequiredDocumentTypeInput,
    RuleOutcomeEnum,
    ValidationInput,
    ValidationOutcomeEnum,
    ValidationOutput,
    ValidationRuleResult,
    round_money,
)


def _serialize_date(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _serialize_decimal(val: Decimal | None) -> str | None:
    return str(val) if val is not None else None


def evaluate_validation(val_input: ValidationInput) -> ValidationOutput:
    """Evaluate pure validation rules V01-V13 deterministically against input state."""
    rules_cfg: dict[str, Any] = val_input.ruleset.get("validation_rules", {})
    ruleset_ver: str = val_input.ruleset.get("ruleset_version", "v1")
    engine_ver: str = val_input.ruleset.get("engine_version", "1.0.0")
    limit_policy: str = val_input.ruleset.get("claim_limit_policy", "CAP_WITH_WARNING")

    results: list[ValidationRuleResult] = []
    blocked_docs: list[str] = []

    # Sort inputs for strict deterministic iteration order
    sorted_coverages: list[CoverageInput] = sorted(val_input.coverages, key=lambda c: c.coverage_id)
    sorted_exclusions: list[ExclusionInput] = sorted(
        val_input.exclusions, key=lambda e: e.exclusion_code
    )
    sorted_documents: list[DocumentInput] = sorted(val_input.documents, key=lambda d: d.document_id)
    sorted_req_docs: list[RequiredDocumentTypeInput] = sorted(
        val_input.required_document_types, key=lambda r: r.doc_code
    )

    claim = val_input.claim
    policy = val_input.policy
    eval_date = val_input.evaluation_date

    # Find matching coverage for the claim category
    matching_coverage: CoverageInput | None = None
    for cov in sorted_coverages:
        if cov.claim_category == claim.claim_category:
            matching_coverage = cov
            break

    # ==========================================
    # V01: Policy Exists & Belongs
    # ==========================================
    cfg_v01 = rules_cfg.get("V01", {})
    v01_ver = str(cfg_v01.get("version", "1.0"))
    v01_snapshot = {
        "claim_policy_id": claim.policy_id,
        "claim_customer_id": claim.customer_id,
        "policy_id": policy.policy_id if policy else None,
        "policy_customer_id": policy.customer_id if policy else None,
    }
    if policy is None:
        results.append(
            ValidationRuleResult(
                rule_id="V01",
                rule_version=v01_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v01.get("reason_code_not_found", "POLICY_NOT_FOUND"),
                message=f"Policy '{claim.policy_id}' not found",
                inputs_snapshot=v01_snapshot,
            )
        )
    elif policy.customer_id != claim.customer_id:
        results.append(
            ValidationRuleResult(
                rule_id="V01",
                rule_version=v01_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v01.get("reason_code_mismatch", "POLICY_OWNER_MISMATCH"),
                message="Policy does not belong to the claiming customer",
                inputs_snapshot=v01_snapshot,
            )
        )
    else:
        results.append(
            ValidationRuleResult(
                rule_id="V01",
                rule_version=v01_ver,
                outcome=RuleOutcomeEnum.PASS,
                reason_code=None,
                message="Policy exists and customer matches",
                inputs_snapshot=v01_snapshot,
            )
        )

    # ==========================================
    # V02: Policy Active
    # ==========================================
    cfg_v02 = rules_cfg.get("V02", {})
    v02_ver = str(cfg_v02.get("version", "1.0"))
    v02_snapshot = {
        "status": policy.status if policy else None,
        "evaluation_date": _serialize_date(eval_date),
    }
    if policy is None:
        results.append(
            ValidationRuleResult(
                rule_id="V02",
                rule_version=v02_ver,
                outcome=RuleOutcomeEnum.SKIPPED,
                reason_code=None,
                message="Skipped because policy was not found",
                inputs_snapshot=v02_snapshot,
            )
        )
    elif policy.status != "ACTIVE":
        results.append(
            ValidationRuleResult(
                rule_id="V02",
                rule_version=v02_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v02.get("reason_code", "POLICY_NOT_ACTIVE"),
                message=f"Policy status is '{policy.status}', but ACTIVE is required",
                inputs_snapshot=v02_snapshot,
            )
        )
    else:
        results.append(
            ValidationRuleResult(
                rule_id="V02",
                rule_version=v02_ver,
                outcome=RuleOutcomeEnum.PASS,
                reason_code=None,
                message="Policy is active",
                inputs_snapshot=v02_snapshot,
            )
        )

    # ==========================================
    # V03: Incident in Policy Period
    # ==========================================
    cfg_v03 = rules_cfg.get("V03", {})
    v03_ver = str(cfg_v03.get("version", "1.0"))
    v03_snapshot = {
        "incident_date": _serialize_date(claim.incident_date),
        "start_date": _serialize_date(policy.start_date) if policy else None,
        "end_date": _serialize_date(policy.end_date) if policy else None,
    }
    if policy is None:
        results.append(
            ValidationRuleResult(
                rule_id="V03",
                rule_version=v03_ver,
                outcome=RuleOutcomeEnum.SKIPPED,
                reason_code=None,
                message="Skipped because policy was not found",
                inputs_snapshot=v03_snapshot,
            )
        )
    elif not (policy.start_date <= claim.incident_date <= policy.end_date):
        results.append(
            ValidationRuleResult(
                rule_id="V03",
                rule_version=v03_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v03.get("reason_code", "INCIDENT_OUTSIDE_POLICY_PERIOD"),
                message=f"Incident date {claim.incident_date} falls outside policy period ({policy.start_date} to {policy.end_date})",
                inputs_snapshot=v03_snapshot,
            )
        )
    else:
        results.append(
            ValidationRuleResult(
                rule_id="V03",
                rule_version=v03_ver,
                outcome=RuleOutcomeEnum.PASS,
                reason_code=None,
                message="Incident date is within policy period",
                inputs_snapshot=v03_snapshot,
            )
        )

    # ==========================================
    # V04: Valid Incident Date
    # ==========================================
    cfg_v04 = rules_cfg.get("V04", {})
    v04_ver = str(cfg_v04.get("version", "1.0"))
    v04_snapshot = {
        "incident_date": _serialize_date(claim.incident_date),
        "evaluation_date": _serialize_date(eval_date),
        "registered_date": _serialize_date(claim.registered_date),
    }
    if claim.incident_date > eval_date:
        results.append(
            ValidationRuleResult(
                rule_id="V04",
                rule_version=v04_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v04.get("reason_code", "INCIDENT_DATE_INVALID"),
                message=f"Incident date {claim.incident_date} is in the future relative to evaluation date {eval_date}",
                inputs_snapshot=v04_snapshot,
            )
        )
    elif claim.incident_date > claim.registered_date:
        results.append(
            ValidationRuleResult(
                rule_id="V04",
                rule_version=v04_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v04.get("reason_code", "INCIDENT_DATE_INVALID"),
                message=f"Incident date {claim.incident_date} is after registration date {claim.registered_date}",
                inputs_snapshot=v04_snapshot,
            )
        )
    else:
        results.append(
            ValidationRuleResult(
                rule_id="V04",
                rule_version=v04_ver,
                outcome=RuleOutcomeEnum.PASS,
                reason_code=None,
                message="Incident date is valid",
                inputs_snapshot=v04_snapshot,
            )
        )

    # ==========================================
    # V05: Filing Window
    # ==========================================
    cfg_v05 = rules_cfg.get("V05", {})
    v05_ver = str(cfg_v05.get("version", "1.0"))
    max_filing_days = int(cfg_v05.get("filing_window_days", 90))
    filing_delta_days = (claim.registered_date - claim.incident_date).days
    v05_snapshot = {
        "incident_date": _serialize_date(claim.incident_date),
        "registered_date": _serialize_date(claim.registered_date),
        "days_elapsed": filing_delta_days,
        "max_filing_days": max_filing_days,
    }
    if filing_delta_days > max_filing_days:
        results.append(
            ValidationRuleResult(
                rule_id="V05",
                rule_version=v05_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v05.get("reason_code", "FILING_WINDOW_EXCEEDED"),
                message=f"Claim filed {filing_delta_days} days after incident, exceeding {max_filing_days} days limit",
                inputs_snapshot=v05_snapshot,
            )
        )
    else:
        results.append(
            ValidationRuleResult(
                rule_id="V05",
                rule_version=v05_ver,
                outcome=RuleOutcomeEnum.PASS,
                reason_code=None,
                message="Filing window requirement satisfied",
                inputs_snapshot=v05_snapshot,
            )
        )

    # ==========================================
    # V07: Coverage Exists & Not Excluded
    # ==========================================
    cfg_v07 = rules_cfg.get("V07", {})
    v07_ver = str(cfg_v07.get("version", "1.0"))
    v07_snapshot = {
        "claim_category": claim.claim_category,
        "coverage_found": matching_coverage is not None,
        "is_excluded": matching_coverage.is_excluded if matching_coverage else None,
    }
    if matching_coverage is None:
        results.append(
            ValidationRuleResult(
                rule_id="V07",
                rule_version=v07_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v07.get("reason_code", "NO_COVERAGE"),
                message=f"No coverage defined for claim category '{claim.claim_category}'",
                inputs_snapshot=v07_snapshot,
            )
        )
    elif matching_coverage.is_excluded:
        results.append(
            ValidationRuleResult(
                rule_id="V07",
                rule_version=v07_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v07.get("reason_code", "NO_COVERAGE"),
                message=f"Coverage for '{claim.claim_category}' is explicitly marked excluded",
                inputs_snapshot=v07_snapshot,
            )
        )
    else:
        results.append(
            ValidationRuleResult(
                rule_id="V07",
                rule_version=v07_ver,
                outcome=RuleOutcomeEnum.PASS,
                reason_code=None,
                message="Active coverage exists for category",
                inputs_snapshot=v07_snapshot,
            )
        )

    # ==========================================
    # V08: Policy Exclusion Check
    # ==========================================
    cfg_v08 = rules_cfg.get("V08", {})
    v08_ver = str(cfg_v08.get("version", "1.0"))
    matched_exclusion: ExclusionInput | None = None
    for excl in sorted_exclusions:
        if excl.exclusion_code == claim.claim_category:
            matched_exclusion = excl
            break

    v08_snapshot = {
        "claim_category": claim.claim_category,
        "matched_exclusion_code": matched_exclusion.exclusion_code if matched_exclusion else None,
    }
    if matched_exclusion:
        results.append(
            ValidationRuleResult(
                rule_id="V08",
                rule_version=v08_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v08.get("reason_code", "EXCLUDED"),
                message=f"Claim category '{claim.claim_category}' matches exclusion: {matched_exclusion.description}",
                inputs_snapshot=v08_snapshot,
            )
        )
    else:
        results.append(
            ValidationRuleResult(
                rule_id="V08",
                rule_version=v08_ver,
                outcome=RuleOutcomeEnum.PASS,
                reason_code=None,
                message="No matching policy exclusions found",
                inputs_snapshot=v08_snapshot,
            )
        )

    # ==========================================
    # V09: Waiting Period Satisfied
    # ==========================================
    cfg_v09 = rules_cfg.get("V09", {})
    v09_ver = str(cfg_v09.get("version", "1.0"))
    v09_snapshot: dict[str, Any]
    if policy is None or matching_coverage is None:
        v09_snapshot = {
            "policy_present": policy is not None,
            "coverage_present": matching_coverage is not None,
        }
        results.append(
            ValidationRuleResult(
                rule_id="V09",
                rule_version=v09_ver,
                outcome=RuleOutcomeEnum.SKIPPED,
                reason_code=None,
                message="Skipped due to missing policy or coverage",
                inputs_snapshot=v09_snapshot,
            )
        )
    else:
        waiting_days = matching_coverage.waiting_period_days
        eligible_date = policy.start_date + timedelta(days=waiting_days)
        v09_snapshot = {
            "incident_date": _serialize_date(claim.incident_date),
            "policy_start": _serialize_date(policy.start_date),
            "waiting_period_days": waiting_days,
            "eligible_date": _serialize_date(eligible_date),
        }
        if claim.incident_date < eligible_date:
            results.append(
                ValidationRuleResult(
                    rule_id="V09",
                    rule_version=v09_ver,
                    outcome=RuleOutcomeEnum.FAIL,
                    reason_code=cfg_v09.get("reason_code", "WAITING_PERIOD_NOT_MET"),
                    message=f"Incident occurred before waiting period ended ({waiting_days} days)",
                    inputs_snapshot=v09_snapshot,
                )
            )
        else:
            results.append(
                ValidationRuleResult(
                    rule_id="V09",
                    rule_version=v09_ver,
                    outcome=RuleOutcomeEnum.PASS,
                    reason_code=None,
                    message="Waiting period satisfied",
                    inputs_snapshot=v09_snapshot,
                )
            )

    # ==========================================
    # V10: Positive Requested Amount
    # ==========================================
    cfg_v10 = rules_cfg.get("V10", {})
    v10_ver = str(cfg_v10.get("version", "1.0"))
    v10_snapshot = {"requested_amount": _serialize_decimal(claim.requested_amount)}
    if claim.requested_amount <= Decimal("0.00"):
        results.append(
            ValidationRuleResult(
                rule_id="V10",
                rule_version=v10_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v10.get("reason_code", "AMOUNT_INVALID"),
                message="Requested amount must be greater than zero",
                inputs_snapshot=v10_snapshot,
            )
        )
    else:
        results.append(
            ValidationRuleResult(
                rule_id="V10",
                rule_version=v10_ver,
                outcome=RuleOutcomeEnum.PASS,
                reason_code=None,
                message="Requested amount is positive",
                inputs_snapshot=v10_snapshot,
            )
        )

    # ==========================================
    # V11: Coverage Limit Check (Section A.1 Policy)
    # ==========================================
    cfg_v11 = rules_cfg.get("V11", {})
    v11_ver = str(cfg_v11.get("version", "1.0"))
    v11_snapshot: dict[str, Any]
    if matching_coverage is None:
        v11_snapshot = {"coverage_present": False}
        results.append(
            ValidationRuleResult(
                rule_id="V11",
                rule_version=v11_ver,
                outcome=RuleOutcomeEnum.SKIPPED,
                reason_code=None,
                message="Skipped because coverage is not found",
                inputs_snapshot=v11_snapshot,
            )
        )
    else:
        remaining_capacity = matching_coverage.limit_amount - matching_coverage.consumed_amount
        remaining_limit = round_money(max(Decimal("0.00"), remaining_capacity))
        exceeds_limit = claim.requested_amount > remaining_limit
        v11_snapshot = {
            "requested_amount": _serialize_decimal(claim.requested_amount),
            "limit_amount": _serialize_decimal(matching_coverage.limit_amount),
            "consumed_amount": _serialize_decimal(matching_coverage.consumed_amount),
            "remaining_limit": _serialize_decimal(remaining_limit),
            "policy": limit_policy,
            "limit_warning": exceeds_limit,
        }

        if exceeds_limit and limit_policy == "REJECT":
            results.append(
                ValidationRuleResult(
                    rule_id="V11",
                    rule_version=v11_ver,
                    outcome=RuleOutcomeEnum.FAIL,
                    reason_code=cfg_v11.get("reason_code", "LIMIT_EXCEEDED"),
                    message=f"Requested amount exceeds remaining limit of {remaining_limit}",
                    inputs_snapshot=v11_snapshot,
                )
            )
        elif exceeds_limit:
            # Proposed Section A.1 policy: pass with warning snapshot and cap payable at approval
            results.append(
                ValidationRuleResult(
                    rule_id="V11",
                    rule_version=v11_ver,
                    outcome=RuleOutcomeEnum.PASS,
                    reason_code=None,
                    message=f"Requested amount exceeds remaining limit {remaining_limit}; payable will be capped at approval",
                    inputs_snapshot=v11_snapshot,
                )
            )
        else:
            results.append(
                ValidationRuleResult(
                    rule_id="V11",
                    rule_version=v11_ver,
                    outcome=RuleOutcomeEnum.PASS,
                    reason_code=None,
                    message="Requested amount is within remaining coverage limit",
                    inputs_snapshot=v11_snapshot,
                )
            )

    # ==========================================
    # V12: Mandatory Fields Present
    # ==========================================
    cfg_v12 = rules_cfg.get("V12", {})
    v12_ver = str(cfg_v12.get("version", "1.0"))
    missing_fields: list[str] = []
    if not claim.incident_location or not claim.incident_location.strip():
        missing_fields.append("incident_location")
    if not claim.incident_description or not claim.incident_description.strip():
        missing_fields.append("incident_description")
    if claim.requested_amount is None:
        missing_fields.append("requested_amount")
    if claim.incident_date is None:
        missing_fields.append("incident_date")

    v12_snapshot = {
        "missing_fields": missing_fields,
        "fields_checked": [
            "incident_date",
            "incident_location",
            "incident_description",
            "requested_amount",
        ],
    }
    if missing_fields:
        results.append(
            ValidationRuleResult(
                rule_id="V12",
                rule_version=v12_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=cfg_v12.get("reason_code", "MISSING_FIELD"),
                message=f"Mandatory claim fields missing: {', '.join(missing_fields)}",
                inputs_snapshot=v12_snapshot,
            )
        )
    else:
        results.append(
            ValidationRuleResult(
                rule_id="V12",
                rule_version=v12_ver,
                outcome=RuleOutcomeEnum.PASS,
                reason_code=None,
                message="All mandatory fields are present",
                inputs_snapshot=v12_snapshot,
            )
        )

    # ==========================================
    # V13: Document Lifecycle (Section B.14)
    # ==========================================
    cfg_v13 = rules_cfg.get("V13", {})
    v13_ver = str(cfg_v13.get("version", "1.0"))
    v13_failed_reason: str | None = None
    v13_message: str = "All mandatory documents verified valid"
    doc_type_evaluations: dict[str, str] = {}

    # Map uploaded docs by code
    docs_by_code: dict[str, list[DocumentInput]] = {}
    for doc in sorted_documents:
        docs_by_code.setdefault(doc.doc_code, []).append(doc)

    for req in sorted_req_docs:
        if not req.is_mandatory:
            continue

        attached_docs = docs_by_code.get(req.doc_code, [])
        has_valid = any(d.status == "VALID" for d in attached_docs)
        if has_valid:
            doc_type_evaluations[req.doc_code] = "VALID"
            continue

        # Check why it's not valid: missing, pending review, or invalid (Section B.15)
        if not attached_docs:
            code = cfg_v13.get("reason_code_missing", "DOCUMENTS_MISSING")
            blocked_reason = f"Mandatory document '{req.name}' ({req.doc_code}) is missing"
        elif any(d.status == "PENDING_REVIEW" for d in attached_docs):
            code = cfg_v13.get("reason_code_pending", "DOCUMENT_PENDING_REVIEW")
            blocked_reason = f"Mandatory document '{req.name}' ({req.doc_code}) is pending review"
        elif all(d.status == "INVALID" for d in attached_docs):
            code = cfg_v13.get("reason_code_invalid", "DOCUMENT_INVALID")
            blocked_reason = f"Mandatory document '{req.name}' ({req.doc_code}) was marked invalid"
        else:
            code = cfg_v13.get("reason_code_missing", "DOCUMENTS_MISSING")
            blocked_reason = f"Mandatory document '{req.name}' ({req.doc_code}) is not valid"

        doc_type_evaluations[req.doc_code] = code
        blocked_docs.append(blocked_reason)
        if v13_failed_reason is None:
            v13_failed_reason = code

    v13_snapshot = {
        "mandatory_doc_evaluations": doc_type_evaluations,
        "blocked_reasons": blocked_docs,
    }

    if blocked_docs:
        v13_message = f"{len(blocked_docs)} mandatory document(s) missing or unverified"
        results.append(
            ValidationRuleResult(
                rule_id="V13",
                rule_version=v13_ver,
                outcome=RuleOutcomeEnum.FAIL,
                reason_code=v13_failed_reason,
                message=v13_message,
                inputs_snapshot=v13_snapshot,
            )
        )
    else:
        results.append(
            ValidationRuleResult(
                rule_id="V13",
                rule_version=v13_ver,
                outcome=RuleOutcomeEnum.PASS,
                reason_code=None,
                message=v13_message,
                inputs_snapshot=v13_snapshot,
            )
        )

    # ==========================================
    # Overall Outcome Resolution
    # ==========================================
    failing_rules = [r for r in results if r.outcome == RuleOutcomeEnum.FAIL]

    if not failing_rules:
        overall = ValidationOutcomeEnum.PASS
    elif len(failing_rules) == 1 and failing_rules[0].rule_id == "V13":
        # Section 9 / Section B: Only V13 failing produces BLOCKED_DOCS
        overall = ValidationOutcomeEnum.BLOCKED_DOCS
    else:
        overall = ValidationOutcomeEnum.FAIL

    return ValidationOutput(
        ruleset_version=ruleset_ver,
        engine_version=engine_ver,
        evaluated_on=eval_date,
        overall_result=overall,
        rule_results=results,
        blocked_document_reasons=blocked_docs,
    )

"""Wording and no auto-rejection tests (P6 Task 11).

1. Scans templates, API messages, schemas, and exports for fraud-declaring words.
2. Proves no code path turns a risk score or category into REJECTED.
"""

from __future__ import annotations

import ast
import re
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db.models import Claim, Coverage, Customer, Policy, User
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.services.decision_service import DecisionService
from app.services.risk_service import RiskService

FORBIDDEN_FRAUD_DECLARING_PATTERNS = [
    re.compile(r"\bfraudster\b", re.IGNORECASE),
    re.compile(r"\bfraudulent\b", re.IGNORECASE),
    re.compile(r"\bfraud\s+detected\b", re.IGNORECASE),
    re.compile(r"\bconfirmed\s+fraud\b", re.IGNORECASE),
    re.compile(r"\bguilty\b", re.IGNORECASE),
    re.compile(r"\bcriminal\b", re.IGNORECASE),
    re.compile(r"\bscammer\b", re.IGNORECASE),
]


def test_no_fraud_declaring_words_in_user_facing_code() -> None:
    """Scan API messages, templates, and schemas for prohibited fraud-declaring words."""
    scan_dirs = [
        Path("app/api"),
        Path("app/schemas"),
        Path("app/templates"),
        Path("app/reports"),
    ]

    violations: list[str] = []

    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for file_path in scan_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix not in (".py", ".html", ".jinja2", ".json", ".txt"):
                continue

            content = file_path.read_text(encoding="utf-8", errors="ignore")
            for line_no, line in enumerate(content.splitlines(), start=1):
                # Skip comments in python files
                stripped = line.strip()
                if file_path.suffix == ".py" and stripped.startswith("#"):
                    continue

                for pattern in FORBIDDEN_FRAUD_DECLARING_PATTERNS:
                    if pattern.search(line):
                        violations.append(
                            f"{file_path}:{line_no} -> '{stripped}' matches {pattern.pattern}"
                        )

    assert not violations, "Found prohibited fraud-declaring wording:\n" + "\n".join(violations)


def test_ast_proves_risk_engine_and_service_never_set_rejected() -> None:
    """Inspect AST of risk engine and risk service to prove they never set status to REJECTED."""
    files_to_check = [
        Path("app/engine/risk.py"),
        Path("app/services/risk_service.py"),
    ]

    for file_path in files_to_check:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            # Check for assignments like claim.status = ClaimStatus.REJECTED or "REJECTED"
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "status":
                        if isinstance(node.value, ast.Attribute) and node.value.attr == "REJECTED":
                            pytest.fail(f"{file_path}:{node.lineno} sets status to REJECTED")
                        if isinstance(node.value, ast.Constant) and node.value.value == "REJECTED":
                            pytest.fail(f"{file_path}:{node.lineno} sets status to 'REJECTED'")


def test_high_risk_claim_is_never_auto_rejected(db_session: Session) -> None:
    """A claim triggering high risk or sufficient_alone is routed to investigation, never REJECTED."""
    customer = Customer(
        id=uuid.uuid4(),
        customer_no=f"CUST-NR-{uuid.uuid4().hex[:6]}",
        full_name="Rajesh Sen",
        dob=date(1985, 3, 15),
        email=f"rajesh_{uuid.uuid4().hex[:6]}@example.com",
        phone="+919876543299",
        address="44 Ballygunge, Kolkata",
        state_region="West Bengal",
    )
    db_session.add(customer)

    policy = Policy(
        id=uuid.uuid4(),
        policy_no=f"POL-NR-{uuid.uuid4().hex[:6]}",
        customer_id=customer.id,
        product_type=ProductType.HEALTH,
        status=PolicyStatus.ACTIVE,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 1),
        premium=Decimal("12000.00"),
        sum_insured=Decimal("500000.00"),
    )
    db_session.add(policy)

    coverage = Coverage(
        id=uuid.uuid4(),
        policy_id=policy.id,
        claim_category="INPATIENT",
        limit_amount=Decimal("500000.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        limit_exceeded_behavior="CAP",
    )
    db_session.add(coverage)

    staff_user = User(
        id=uuid.uuid4(),
        email=f"staff_{uuid.uuid4().hex[:6]}@example.com",
        role=UserRole.STAFF,
        password_hash="mock",
    )
    assessor_user = User(
        id=uuid.uuid4(),
        email=f"assessor_{uuid.uuid4().hex[:6]}@example.com",
        role=UserRole.ASSESSOR,
        password_hash="mock",
    )
    db_session.add_all([staff_user, assessor_user])
    db_session.flush()

    # Claim registered within 30 days of inception (R05: 20) and requested_amount is 90% of sum_insured (R03: 20)
    # Total score = 40 >= 40 threshold -> routes to UNDER_INVESTIGATION
    claim = Claim(
        id=uuid.uuid4(),
        claim_ref=f"CLM-NR-{uuid.uuid4().hex[:4]}",
        customer_id=customer.id,
        policy_id=policy.id,
        incident_date=date(2025, 1, 10),
        claim_category="INPATIENT",
        incident_location="Kolkata",
        incident_description="Emergency surgery within 10 days of policy start",
        requested_amount=Decimal("450000.00"),
        registered_by=staff_user.id,
        channel=ClaimChannel.STAFF,
        status=ClaimStatus.VALIDATED,
    )
    db_session.add(claim)
    db_session.commit()

    risk_service = RiskService(db_session)
    eval_run = risk_service.evaluate_claim_risk(claim_id=claim.id, actor=assessor_user)

    db_session.refresh(claim)

    # Claim must NEVER be in REJECTED status
    assert claim.status != ClaimStatus.REJECTED
    assert claim.status == ClaimStatus.UNDER_INVESTIGATION
    assert eval_run.requires_investigation is True

    # And decision status cannot be REJECTED without an explicit human call with reason codes
    decision_service = DecisionService(db_session)
    decisions = decision_service.get_decisions(claim_id=claim.id, actor=assessor_user)
    assert len(decisions) == 0

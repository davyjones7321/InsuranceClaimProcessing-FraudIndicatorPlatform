"""Service-level authorization AST verification and functional tests (P1 Task 1, Fix F3)."""

import ast
import glob
import os
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.db.models import Claim, Customer, Policy, User
from app.domain.enums import ClaimChannel, ClaimStatus, PolicyStatus, ProductType, UserRole
from app.schemas.assessments import AssessmentCreate
from app.schemas.claims import ClaimStatusTransitionRequest
from app.schemas.decisions import DecisionCreate
from app.security.password import hash_password
from app.security.rbac import PermissionDeniedError
from app.services.assessment_service import AssessmentPermissionError, AssessmentService
from app.services.claim_service import ClaimService
from app.services.decision_service import DecisionPermissionError, DecisionService
from app.services.investigation_service import InvestigationService
from app.services.risk_service import RiskService


def test_ast_service_methods_enforce_claim_authorization() -> None:
    """Inspect AST of all services to guarantee every claim-scoped public method invokes authorization."""
    service_files = glob.glob(os.path.join("app", "services", "*_service.py"))
    assert len(service_files) >= 5, "Expected at least 5 domain service files"

    auth_check_names = {
        "check_claim_access",
        "check_document_access",
        "check_risk_access",
        "get_active_investigation",
    }

    exempt_methods = {
        ("__init__",),
        ("generate_claim_ref",),
        ("build_validation_input",),
        ("build_risk_input",),
        ("import_json",),
        ("import_csv",),
    }

    for file_path in service_files:
        with open(file_path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=file_path)

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        func_name = item.name
                        if func_name.startswith("_") or (func_name,) in exempt_methods:
                            continue

                        arg_names = [a.arg for a in item.args.args]
                        # Check if method is claim-scoped
                        if "claim_id" in arg_names or "claim" in arg_names:
                            # Verify that at least one statement or called function is an auth check
                            called_names = set()
                            for subnode in ast.walk(item):
                                if isinstance(subnode, ast.Call):
                                    if isinstance(subnode.func, ast.Name):
                                        called_names.add(subnode.func.id)
                                    elif isinstance(subnode.func, ast.Attribute):
                                        called_names.add(subnode.func.attr)

                            has_auth_check = bool(called_names.intersection(auth_check_names))
                            assert has_auth_check, (
                                f"Service method '{node.name}.{func_name}' in {file_path} touches a claim "
                                f"but does not invoke any service-level authorization check "
                                f"({auth_check_names}). Fix F3."
                            )


@pytest.fixture
def service_auth_setup(db_session: Session) -> dict[str, Any]:
    """Create test data for service-level authorization tests."""
    cust_owner = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-SA-001",
        full_name="Alice Owner",
        dob=date(1990, 1, 1),
        email="alice.owner@example.com",
        phone="+15551111",
        address="100 Main St",
        state_region="CA",
    )
    cust_stranger = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-SA-002",
        full_name="Bob Stranger",
        dob=date(1991, 2, 2),
        email="bob.stranger@example.com",
        phone="+15552222",
        address="200 Elm St",
        state_region="CA",
    )

    user_owner = User(
        id=uuid.uuid4(),
        email="alice.owner@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.CUSTOMER,
        customer_id=cust_owner.id,
        is_active=True,
    )
    user_stranger = User(
        id=uuid.uuid4(),
        email="bob.stranger@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.CUSTOMER,
        customer_id=cust_stranger.id,
        is_active=True,
    )
    user_staff_a = User(
        id=uuid.uuid4(),
        email="staff.a@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.STAFF,
        is_active=True,
    )
    user_staff_b = User(
        id=uuid.uuid4(),
        email="staff.b@example.com",
        password_hash=hash_password("Password123!"),
        role=UserRole.STAFF,
        is_active=True,
    )

    policy = Policy(
        id=uuid.uuid4(),
        policy_no="POL-SA-001",
        customer_id=cust_owner.id,
        product_type=ProductType.HEALTH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("15000.00"),
        sum_insured=Decimal("500000.00"),
        status=PolicyStatus.ACTIVE,
    )

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-2026-000999",
        policy_id=policy.id,
        customer_id=cust_owner.id,
        claim_category="HOSPITALIZATION",
        incident_date=date(2026, 4, 1),
        incident_location="City Clinic",
        incident_description="Medical treatment.",
        requested_amount=Decimal("45000.00"),
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.REGISTERED,
        registered_by=user_staff_a.id,
    )

    db_session.add_all(
        [
            cust_owner,
            cust_stranger,
            user_owner,
            user_stranger,
            user_staff_a,
            user_staff_b,
            policy,
            claim,
        ]
    )
    db_session.commit()

    return {
        "user_owner": user_owner,
        "user_stranger": user_stranger,
        "user_staff_a": user_staff_a,
        "user_staff_b": user_staff_b,
        "claim": claim,
    }


def test_service_level_authorization_blocks_stranger_customer(
    db_session: Session, service_auth_setup: dict[str, Any]
) -> None:
    """Invoking service methods directly with a non-owner customer fails closed."""
    stranger: User = service_auth_setup["user_stranger"]
    claim: Claim = service_auth_setup["claim"]

    claim_service = ClaimService(db_session)
    decision_service = DecisionService(db_session)
    assessment_service = AssessmentService(db_session)
    investigation_service = InvestigationService(db_session)
    risk_service = RiskService(db_session)

    # 1. Claim transition fails
    req = ClaimStatusTransitionRequest(
        to_status=ClaimStatus.WITHDRAWN,
        expected_status_version=claim.status_version,
    )
    with pytest.raises(PermissionDeniedError):
        claim_service.transition_status(claim.id, req, actor=stranger)

    # 2. Decision service fails
    dec_req = DecisionCreate(
        decision=ClaimStatus.REJECTED,
        remarks="Attempted unauthorized direct decision",
        reason_code="OTHER",
    )
    with pytest.raises(DecisionPermissionError):
        decision_service.make_decision(claim.id, dec_req, actor=stranger)

    # 3. Assessment service fails
    ass_req = AssessmentCreate(
        assessed_loss=Decimal("10000.00"),
        eligible_amount=Decimal("8000.00"),
        remarks="Unauthorized assessment remarks",
    )
    with pytest.raises(AssessmentPermissionError):
        assessment_service.create_assessment(claim.id, ass_req, actor=stranger)

    # 4. Investigation service fails
    with pytest.raises(PermissionDeniedError):
        investigation_service.start_review(claim.id, actor=stranger)

    # 5. Risk service fails
    with pytest.raises(PermissionDeniedError):
        risk_service.get_risk_evaluations(claim.id, actor=stranger)


def test_service_level_authorization_blocks_unauthorized_staff(
    db_session: Session, service_auth_setup: dict[str, Any]
) -> None:
    """Staff B cannot mutate a claim registered by Staff A directly in the service."""
    staff_b: User = service_auth_setup["user_staff_b"]
    claim: Claim = service_auth_setup["claim"]

    claim_service = ClaimService(db_session)
    req = ClaimStatusTransitionRequest(
        to_status=ClaimStatus.DOCUMENTS_PENDING,
        expected_status_version=claim.status_version,
    )

    with pytest.raises(PermissionDeniedError, match="Staff can only access"):
        claim_service.transition_status(claim.id, req, actor=staff_b)

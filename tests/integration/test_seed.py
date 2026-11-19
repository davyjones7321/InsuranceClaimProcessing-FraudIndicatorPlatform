"""Integration tests for deterministic database seeding script."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    AuditLog,
    Claim,
    Coverage,
    Customer,
    Exclusion,
    Policy,
    PolicyInsuredItem,
    RequiredDocumentType,
    User,
)
from app.domain.enums import ClaimStatus, PolicyStatus, ProductType, UserRole
from scripts.seed import seed_database


def test_seed_database_creates_all_expected_records(db_session: Session) -> None:
    """Verify seed populates users, 25 customers, policies across all products, and configurations."""
    seed_database(db_session)

    # 1. Verify Customers count
    customer_count = db_session.scalar(select(func.count(Customer.id)))
    assert customer_count == 25

    # 2. Verify Users across all required roles
    roles_present = set(db_session.scalars(select(User.role)).all())
    expected_roles = {
        UserRole.ADMIN,
        UserRole.MANAGER,
        UserRole.INVESTIGATOR,
        UserRole.ASSESSOR,
        UserRole.STAFF,
        UserRole.CUSTOMER,
    }
    assert expected_roles.issubset(roles_present)

    # 3. Verify Policies across HEALTH, VEHICLE, PROPERTY
    product_types = set(db_session.scalars(select(Policy.product_type)).all())
    assert product_types == {ProductType.HEALTH, ProductType.VEHICLE, ProductType.PROPERTY}

    # 4. Verify Policy statuses (including ACTIVE, LAPSED, CANCELLED, EXPIRED)
    statuses = set(db_session.scalars(select(Policy.status)).all())
    expected_statuses = {
        PolicyStatus.ACTIVE,
        PolicyStatus.LAPSED,
        PolicyStatus.CANCELLED,
        PolicyStatus.EXPIRED,
    }
    assert expected_statuses.issubset(statuses)

    # 5. Verify Coverages, Exclusions, Insured Items exist
    coverage_count = db_session.scalar(select(func.count(Coverage.id)))
    assert coverage_count and coverage_count >= 10

    exclusion_count = db_session.scalar(select(func.count(Exclusion.id)))
    assert exclusion_count and exclusion_count >= 5

    insured_item_count = db_session.scalar(select(func.count(PolicyInsuredItem.id)))
    assert insured_item_count and insured_item_count >= 10

    # 6. Verify Required Document Types
    doc_types_count = db_session.scalar(select(func.count(RequiredDocumentType.id)))
    assert doc_types_count and doc_types_count >= 10

    # 7. Verify Historical Claims (200+ across all statuses)
    claim_count = db_session.scalar(select(func.count(Claim.id)))
    assert claim_count and claim_count >= 200

    claim_statuses = set(db_session.scalars(select(Claim.status)).all())
    expected_claim_statuses = {
        ClaimStatus.REGISTERED,
        ClaimStatus.DOCUMENTS_PENDING,
        ClaimStatus.VALIDATION_FAILED,
        ClaimStatus.VALIDATED,
        ClaimStatus.RISK_EVALUATED,
        ClaimStatus.UNDER_INVESTIGATION,
        ClaimStatus.ESCALATED,
        ClaimStatus.INVESTIGATION_COMPLETE,
        ClaimStatus.READY_FOR_ASSESSMENT,
        ClaimStatus.ASSESSED,
        ClaimStatus.APPROVED,
        ClaimStatus.REJECTED,
        ClaimStatus.INFO_REQUESTED,
        ClaimStatus.WITHDRAWN,
    }
    assert expected_claim_statuses.issubset(claim_statuses)

    # 8. Verify Audit log recorded
    seed_audit = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "SEED_DATABASE")
    ).first()
    assert seed_audit is not None
    assert seed_audit.event_data["customers_count"] == 25


def test_seed_database_is_idempotent(db_session: Session) -> None:
    """Running seed multiple times should not raise integrity or duplicate errors."""
    seed_database(db_session)
    # Second run
    seed_database(db_session)

    customer_count = db_session.scalar(select(func.count(Customer.id)))
    assert customer_count == 25


def test_seed_database_refuses_production(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed script must raise RuntimeError when ENV is production."""
    monkeypatch.setattr(settings, "ENV", "production")
    with pytest.raises(RuntimeError, match="must not run in production"):
        seed_database(db_session)

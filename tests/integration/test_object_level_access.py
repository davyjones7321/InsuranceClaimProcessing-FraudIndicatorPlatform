"""Integration tests verifying object-level authorization boundaries for customers."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Coverage, Customer, Exclusion, Policy, User
from app.domain.enums import LimitScope, PolicyStatus, ProductType, UserRole
from app.security.password import hash_password
from app.security.session import create_session


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def two_customers(db_session: Session) -> tuple[tuple[Customer, User], tuple[Customer, User]]:
    """Create two separate customers with their respective user accounts."""
    # Customer 1
    c1 = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-ALPHA-01",
        full_name="Alice Alpha",
        dob=date(1990, 1, 1),
        email="alice@alpha.test",
        phone="+1111111111",
        address="1 Alpha Way",
        state_region="CA",
    )
    db_session.add(c1)
    db_session.flush()

    u1 = User(
        id=uuid.uuid4(),
        email="alice@alpha.test",
        password_hash=hash_password("AliceSecurePass123!"),
        role=UserRole.CUSTOMER,
        customer_id=c1.id,
        is_active=True,
    )
    db_session.add(u1)

    # Customer 2
    c2 = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-BETA-02",
        full_name="Bob Beta",
        dob=date(1992, 2, 2),
        email="bob@beta.test",
        phone="+2222222222",
        address="2 Beta Rd",
        state_region="TX",
    )
    db_session.add(c2)
    db_session.flush()

    u2 = User(
        id=uuid.uuid4(),
        email="bob@beta.test",
        password_hash=hash_password("BobSecurePass123!"),
        role=UserRole.CUSTOMER,
        customer_id=c2.id,
        is_active=True,
    )
    db_session.add(u2)

    # Policy 1 for Customer 1
    p1 = Policy(
        id=uuid.uuid4(),
        policy_no="POL-ALPHA-001",
        customer_id=c1.id,
        product_type=ProductType.HEALTH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("10000.00"),
        sum_insured=Decimal("500000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(p1)
    db_session.flush()

    cov1 = Coverage(
        id=uuid.uuid4(),
        policy_id=p1.id,
        claim_category="HOSPITALIZATION",
        limit_scope=LimitScope.PER_POLICY_PERIOD,
        limit_amount=Decimal("400000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("1000.00"),
        copay_percent=Decimal("10.00"),
        limit_exceeded_behavior="CAP",
    )
    db_session.add(cov1)

    excl1 = Exclusion(
        id=uuid.uuid4(),
        policy_id=p1.id,
        exclusion_code="EXCL_TEST_A",
        description="Alpha exclusion",
    )
    db_session.add(excl1)

    # Policy 2 for Customer 2
    p2 = Policy(
        id=uuid.uuid4(),
        policy_no="POL-BETA-002",
        customer_id=c2.id,
        product_type=ProductType.VEHICLE,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        premium=Decimal("8000.00"),
        sum_insured=Decimal("300000.00"),
        status=PolicyStatus.ACTIVE,
    )
    db_session.add(p2)
    db_session.flush()

    cov2 = Coverage(
        id=uuid.uuid4(),
        policy_id=p2.id,
        claim_category="ACCIDENTAL_DAMAGE",
        limit_scope=LimitScope.PER_POLICY_PERIOD,
        limit_amount=Decimal("300000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("500.00"),
        copay_percent=Decimal("0.00"),
        limit_exceeded_behavior="CAP",
    )
    db_session.add(cov2)

    excl2 = Exclusion(
        id=uuid.uuid4(),
        policy_id=p2.id,
        exclusion_code="EXCL_TEST_B",
        description="Beta exclusion",
    )
    db_session.add(excl2)

    db_session.commit()
    return ((c1, u1), (c2, u2))


def test_customer_cannot_view_other_customer_profile(
    client: TestClient,
    db_session: Session,
    two_customers: tuple[tuple[Customer, User], tuple[Customer, User]],
) -> None:
    (c1, u1), (c2, u2) = two_customers
    u1_hdr = auth_headers(db_session, u1)

    # Alice views own profile: SUCCESS (200)
    res = client.get(f"/api/customers/{c1.id}", headers=u1_hdr)
    assert res.status_code == 200
    assert res.json()["customer_no"] == "CUST-ALPHA-01"

    # Alice attempts to view Bob's profile: FORBIDDEN (403)
    res = client.get(f"/api/customers/{c2.id}", headers=u1_hdr)
    assert res.status_code == 403


def test_customer_cannot_list_all_customers(
    client: TestClient,
    db_session: Session,
    two_customers: tuple[tuple[Customer, User], tuple[Customer, User]],
) -> None:
    (c1, u1), _ = two_customers
    u1_hdr = auth_headers(db_session, u1)

    # Customer role cannot call list_customers (Staff, Manager, Admin only)
    res = client.get("/api/customers", headers=u1_hdr)
    assert res.status_code == 403


def test_customer_cannot_view_other_customer_policy(
    client: TestClient,
    db_session: Session,
    two_customers: tuple[tuple[Customer, User], tuple[Customer, User]],
) -> None:
    (c1, u1), (c2, u2) = two_customers
    u1_hdr = auth_headers(db_session, u1)

    # Retrieve policies from db to get their IDs
    p1 = db_session.query(Policy).filter(Policy.customer_id == c1.id).one()
    p2 = db_session.query(Policy).filter(Policy.customer_id == c2.id).one()

    # Alice views own policy: SUCCESS (200)
    res = client.get(f"/api/policies/{p1.id}", headers=u1_hdr)
    assert res.status_code == 200
    assert res.json()["policy_no"] == "POL-ALPHA-001"

    # Alice attempts to view Bob's policy: FORBIDDEN (403)
    res = client.get(f"/api/policies/{p2.id}", headers=u1_hdr)
    assert res.status_code == 403


def test_customer_policy_list_is_strictly_scoped_to_self(
    client: TestClient,
    db_session: Session,
    two_customers: tuple[tuple[Customer, User], tuple[Customer, User]],
) -> None:
    (c1, u1), (c2, u2) = two_customers
    u1_hdr = auth_headers(db_session, u1)

    # Alice lists policies: only Alice's policy returned
    res = client.get("/api/policies", headers=u1_hdr)
    assert res.status_code == 200
    policies = res.json()
    assert len(policies) == 1
    assert policies[0]["policy_no"] == "POL-ALPHA-001"

    # Alice attempts to query specifically for Bob's customer_id: FORBIDDEN (403)
    res = client.get(f"/api/policies?customer_id={c2.id}", headers=u1_hdr)
    assert res.status_code == 403


def test_customer_cannot_view_other_policy_coverages_or_exclusions(
    client: TestClient,
    db_session: Session,
    two_customers: tuple[tuple[Customer, User], tuple[Customer, User]],
) -> None:
    (c1, u1), (c2, u2) = two_customers
    u1_hdr = auth_headers(db_session, u1)

    p1 = db_session.query(Policy).filter(Policy.customer_id == c1.id).one()
    p2 = db_session.query(Policy).filter(Policy.customer_id == c2.id).one()

    # Alice views own coverages & exclusions: SUCCESS (200)
    res = client.get(f"/api/policies/{p1.id}/coverages", headers=u1_hdr)
    assert res.status_code == 200
    assert len(res.json()) == 1

    res = client.get(f"/api/policies/{p1.id}/exclusions", headers=u1_hdr)
    assert res.status_code == 200
    assert len(res.json()) == 1

    # Alice attempts to view Bob's policy coverages & exclusions: FORBIDDEN (403)
    res = client.get(f"/api/policies/{p2.id}/coverages", headers=u1_hdr)
    assert res.status_code == 403

    res = client.get(f"/api/policies/{p2.id}/exclusions", headers=u1_hdr)
    assert res.status_code == 403

"""Integration tests for Claim Registration, reference generation, and object-level scoping."""

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.models import (
    AuditLog,
    Coverage,
    Customer,
    Policy,
    PolicyInsuredItem,
    User,
)
from app.domain.enums import (
    LimitScope,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.security.password import hash_password
from app.security.session import create_session


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def registration_setup(db_session: Session) -> dict[str, Any]:
    """Setup customers, users, policies, coverages, and insured items."""
    # 1. Customer A + User
    cust_a = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-REG-001",
        full_name="Alice Anderson",
        dob=date(1990, 1, 1),
        email="alice.reg@example.com",
        phone="+15550001",
        address="100 Alice Road",
        state_region="CA",
    )
    user_a = User(
        id=uuid.uuid4(),
        email="alice.reg@example.com",
        password_hash=hash_password("AliceSecurePass123!"),
        role=UserRole.CUSTOMER,
        customer_id=cust_a.id,
        is_active=True,
    )

    # 2. Customer B + User
    cust_b = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-REG-002",
        full_name="Bob Builder",
        dob=date(1985, 5, 20),
        email="bob.reg@example.com",
        phone="+15550002",
        address="200 Bob Lane",
        state_region="NY",
    )
    user_b = User(
        id=uuid.uuid4(),
        email="bob.reg@example.com",
        password_hash=hash_password("BobSecurePass123!"),
        role=UserRole.CUSTOMER,
        customer_id=cust_b.id,
        is_active=True,
    )

    # 3. Staff User
    user_staff = User(
        id=uuid.uuid4(),
        email="staff.reg@example.com",
        password_hash=hash_password("StaffSecurePass123!"),
        role=UserRole.STAFF,
        is_active=True,
    )

    db_session.add_all([cust_a, user_a, cust_b, user_b, user_staff])
    db_session.flush()

    # 4. Active Policy for Customer A
    today = date.today()
    policy_active = Policy(
        id=uuid.uuid4(),
        policy_no="POL-REG-ACT-001",
        customer_id=cust_a.id,
        product_type=ProductType.VEHICLE,
        start_date=today - timedelta(days=60),
        end_date=today + timedelta(days=300),
        premium=Decimal("25000.00"),
        sum_insured=Decimal("800000.00"),
        status=PolicyStatus.ACTIVE,
    )

    # 5. Inactive Policy for Customer A
    policy_lapsed = Policy(
        id=uuid.uuid4(),
        policy_no="POL-REG-LAP-002",
        customer_id=cust_a.id,
        product_type=ProductType.HEALTH,
        start_date=today - timedelta(days=400),
        end_date=today - timedelta(days=35),
        premium=Decimal("15000.00"),
        sum_insured=Decimal("500000.00"),
        status=PolicyStatus.LAPSED,
    )

    db_session.add_all([policy_active, policy_lapsed])
    db_session.flush()

    # 6. Active and Excluded Coverages on active policy
    cov_collision = Coverage(
        id=uuid.uuid4(),
        policy_id=policy_active.id,
        claim_category="COLLISION",
        limit_scope=LimitScope.PER_CLAIM,
        limit_amount=Decimal("500000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("5000.00"),
        copay_percent=Decimal("10.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    cov_flood = Coverage(
        id=uuid.uuid4(),
        policy_id=policy_active.id,
        claim_category="FLOOD_DAMAGE",
        limit_scope=LimitScope.PER_CLAIM,
        limit_amount=Decimal("300000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("10000.00"),
        copay_percent=Decimal("20.00"),
        waiting_period_days=30,
        is_excluded=True,  # Excluded!
        limit_exceeded_behavior="CAP",
    )

    # 7. Insured Vehicle on Active Policy
    insured_car = PolicyInsuredItem(
        id=uuid.uuid4(),
        policy_id=policy_active.id,
        item_type="VEHICLE",
        identifier="VIN-REG-12345",
        details={"make": "Toyota", "model": "Corolla", "year": 2022},
    )

    # 8. Foreign insured item belonging to another policy
    foreign_car = PolicyInsuredItem(
        id=uuid.uuid4(),
        policy_id=policy_lapsed.id,
        item_type="VEHICLE",
        identifier="VIN-FOREIGN-999",
        details={"make": "Honda", "model": "Civic"},
    )

    db_session.add_all([cov_collision, cov_flood, insured_car, foreign_car])
    db_session.commit()

    return {
        "cust_a": cust_a,
        "user_a": user_a,
        "cust_b": cust_b,
        "user_b": user_b,
        "user_staff": user_staff,
        "policy_active": policy_active,
        "policy_lapsed": policy_lapsed,
        "cov_collision": cov_collision,
        "cov_flood": cov_flood,
        "insured_car": insured_car,
        "foreign_car": foreign_car,
    }


def test_customer_registers_claim_successfully(
    client: TestClient, db_session: Session, registration_setup: dict[str, Any]
) -> None:
    """Customer successfully registers a claim on their active policy and receives CLM-YYYY-NNNNNN."""
    user_a = registration_setup["user_a"]
    policy_active = registration_setup["policy_active"]
    insured_car = registration_setup["insured_car"]
    hdr = auth_headers(db_session, user_a)

    today = date.today()
    payload = {
        "policy_id": str(policy_active.id),
        "claim_category": "COLLISION",
        "incident_date": str(today - timedelta(days=5)),
        "incident_location": "Main St & 4th Ave, San Francisco, CA",
        "incident_description": "Minor fender bender while turning at intersection.",
        "requested_amount": "45000.00",
        "insured_item_id": str(insured_car.id),
    }

    res = client.post("/api/claims", headers=hdr, json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "REGISTERED"
    assert data["channel"] == "CUSTOMER"
    assert data["claim_ref"].startswith(f"CLM-{today.year}-")
    assert data["status_version"] == 1
    assert data["customer_id"] == str(registration_setup["cust_a"].id)

    # Verify genesis audit log row created for this claim
    audit_row = db_session.query(AuditLog).filter_by(chain_key=f"claim:{data['id']}", seq=1).first()
    assert audit_row is not None
    assert audit_row.action == "CLAIM_REGISTERED"
    assert audit_row.actor_role == "customer"


def test_customer_cannot_register_claim_on_other_customer_policy(
    client: TestClient, db_session: Session, registration_setup: dict[str, Any]
) -> None:
    """Customer B attempting to register against Customer A's policy must receive 403 Forbidden."""
    user_b = registration_setup["user_b"]
    policy_active = registration_setup["policy_active"]  # Belongs to Customer A
    hdr = auth_headers(db_session, user_b)

    payload = {
        "policy_id": str(policy_active.id),
        "claim_category": "COLLISION",
        "incident_date": str(date.today() - timedelta(days=2)),
        "incident_location": "Highway 101",
        "incident_description": "Unauthorized claim test",
        "requested_amount": "10000.00",
    }

    res = client.post("/api/claims", headers=hdr, json=payload)
    assert res.status_code == 403
    assert (
        "Customers can only register claims against their own active policies"
        in res.json()["detail"]
    )


def test_staff_registers_claim_on_behalf_of_customer(
    client: TestClient, db_session: Session, registration_setup: dict[str, Any]
) -> None:
    """Staff registers a claim on behalf of a customer."""
    user_staff = registration_setup["user_staff"]
    cust_a = registration_setup["cust_a"]
    policy_active = registration_setup["policy_active"]
    insured_car = registration_setup["insured_car"]
    hdr = auth_headers(db_session, user_staff)

    payload = {
        "policy_id": str(policy_active.id),
        "customer_id": str(cust_a.id),
        "claim_category": "COLLISION",
        "incident_date": str(date.today() - timedelta(days=1)),
        "incident_location": "Downtown Garage",
        "incident_description": "Registered via call center staff assistance.",
        "requested_amount": "30000.00",
        "insured_item_id": str(insured_car.id),
    }

    res = client.post("/api/claims", headers=hdr, json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["channel"] == "STAFF"
    assert data["registered_by"] == str(user_staff.id)


def test_claim_registration_validation_guards(
    client: TestClient, db_session: Session, registration_setup: dict[str, Any]
) -> None:
    """Validation guards reject inactive policies, out-of-bounds dates, excluded coverage, and wrong insured item."""
    user_a = registration_setup["user_a"]
    policy_active = registration_setup["policy_active"]
    policy_lapsed = registration_setup["policy_lapsed"]
    foreign_car = registration_setup["foreign_car"]
    hdr = auth_headers(db_session, user_a)

    # 1. Inactive (LAPSED) policy rejected
    res1 = client.post(
        "/api/claims",
        headers=hdr,
        json={
            "policy_id": str(policy_lapsed.id),
            "claim_category": "COLLISION",
            "incident_date": str(date.today() - timedelta(days=5)),
            "incident_location": "City Center",
            "incident_description": "Valid incident description",
            "requested_amount": "5000.00",
        },
    )
    assert res1.status_code == 400
    assert "only ACTIVE policies can receive claims" in res1.json()["detail"]

    # 2. Incident date outside policy period
    res2 = client.post(
        "/api/claims",
        headers=hdr,
        json={
            "policy_id": str(policy_active.id),
            "claim_category": "COLLISION",
            "incident_date": str(date.today() - timedelta(days=200)),  # Before policy start
            "incident_location": "City Center",
            "incident_description": "Valid incident description",
            "requested_amount": "5000.00",
        },
    )
    assert res2.status_code == 400
    assert "falls outside policy period" in res2.json()["detail"]

    # 3. Excluded coverage rejected
    res3 = client.post(
        "/api/claims",
        headers=hdr,
        json={
            "policy_id": str(policy_active.id),
            "claim_category": "FLOOD_DAMAGE",  # Excluded!
            "incident_date": str(date.today() - timedelta(days=2)),
            "incident_location": "Riverside",
            "incident_description": "Water damage description",
            "requested_amount": "15000.00",
        },
    )
    assert res3.status_code == 400
    assert "explicitly excluded" in res3.json()["detail"]

    # 4. Insured item belonging to another policy rejected
    res4 = client.post(
        "/api/claims",
        headers=hdr,
        json={
            "policy_id": str(policy_active.id),
            "claim_category": "COLLISION",
            "incident_date": str(date.today() - timedelta(days=2)),
            "incident_location": "Highway",
            "incident_description": "Crash description",
            "requested_amount": "20000.00",
            "insured_item_id": str(foreign_car.id),
        },
    )
    assert res4.status_code == 400
    assert "does not belong to policy" in res4.json()["detail"]


def test_idempotent_claim_double_submit(
    client: TestClient, db_session: Session, registration_setup: dict[str, Any]
) -> None:
    """Double-submitting with matching source_system and external_ref returns existing claim idempotently."""
    user_a = registration_setup["user_a"]
    policy_active = registration_setup["policy_active"]
    insured_car = registration_setup["insured_car"]
    hdr = auth_headers(db_session, user_a)

    payload = {
        "policy_id": str(policy_active.id),
        "claim_category": "COLLISION",
        "incident_date": str(date.today() - timedelta(days=3)),
        "incident_location": "Market St",
        "incident_description": "Idempotent submission test",
        "requested_amount": "12000.00",
        "source_system": "PORTAL_V1",
        "external_ref": "EXT-TXN-999001",
        "insured_item_id": str(insured_car.id),
    }

    # First submit
    res1 = client.post("/api/claims", headers=hdr, json=payload)
    assert res1.status_code == 201
    claim1 = res1.json()

    # Second submit with identical idempotency keys
    res2 = client.post("/api/claims", headers=hdr, json=payload)
    assert res2.status_code == 201
    claim2 = res2.json()

    assert claim1["id"] == claim2["id"]
    assert claim1["claim_ref"] == claim2["claim_ref"]


def test_claim_listing_object_level_isolation(
    client: TestClient, db_session: Session, registration_setup: dict[str, Any]
) -> None:
    """Customer only sees their own claims; staff sees claims they registered."""
    user_a = registration_setup["user_a"]
    user_b = registration_setup["user_b"]
    user_staff = registration_setup["user_staff"]
    policy_active = registration_setup["policy_active"]
    insured_car = registration_setup["insured_car"]

    hdr_a = auth_headers(db_session, user_a)
    hdr_b = auth_headers(db_session, user_b)
    hdr_staff = auth_headers(db_session, user_staff)

    # Customer A registers a claim
    client.post(
        "/api/claims",
        headers=hdr_a,
        json={
            "policy_id": str(policy_active.id),
            "claim_category": "COLLISION",
            "incident_date": str(date.today() - timedelta(days=1)),
            "incident_location": "Oak St",
            "incident_description": "Claim A",
            "requested_amount": "5000.00",
            "insured_item_id": str(insured_car.id),
        },
    )

    # Customer A lists claims -> has 1
    res_a = client.get("/api/claims", headers=hdr_a)
    assert res_a.status_code == 200
    assert len(res_a.json()) >= 1

    # Customer B lists claims -> sees 0 (object isolation)
    res_b = client.get("/api/claims", headers=hdr_b)
    assert res_b.status_code == 200
    assert len(res_b.json()) == 0

    # Staff lists claims -> sees summary of all claims per D7
    res_staff = client.get("/api/claims", headers=hdr_staff)
    assert res_staff.status_code == 200
    assert len(res_staff.json()) >= 1

    # Staff views full detail of a claim registered by customer -> 403 Forbidden (D7 isolation)
    claim_id = res_a.json()[0]["id"]
    res_staff_detail = client.get(f"/api/claims/{claim_id}", headers=hdr_staff)
    assert res_staff_detail.status_code == 403


def test_staging_smoke_claim_registration_app_rw() -> None:
    """Staging smoke test verifying that an application running as restricted app_rw
    role can successfully register a claim on a migrated PostgreSQL database.
    """
    test_db_url = os.environ.get("TEST_DATABASE_URL")
    if not test_db_url or "postgresql" not in test_db_url:
        pytest.skip("PostgreSQL TEST_DATABASE_URL required for staging app_rw smoke test")

    parsed_url = make_url(test_db_url)
    admin_db_url = parsed_url.set(database="postgres").render_as_string(hide_password=False)
    try:
        admin_engine = create_engine(admin_db_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.fail(f"PostgreSQL server unreachable at {admin_db_url}: {exc}")

    test_db_name = "test_smoke_claim_app_rw_ci"
    with admin_engine.connect() as conn:
        conn.execute(
            text(f"DROP DATABASE IF EXISTS {test_db_name} WITH (FORCE);")
            if conn.dialect.server_version_info >= (13,)
            else text(f"DROP DATABASE IF EXISTS {test_db_name};")
        )
        conn.execute(text(f"CREATE DATABASE {test_db_name};"))

    try:
        # 1. Run deploy/init-db.sql on empty database
        admin_test_url = parsed_url.set(database=test_db_name).render_as_string(hide_password=False)
        admin_test_engine = create_engine(admin_test_url)
        with open("deploy/init-db.sql", encoding="utf-8") as f:
            init_sql = f.read()
        with admin_test_engine.connect() as conn:
            conn.execute(text(init_sql))
            conn.commit()
        admin_test_engine.dispose()

        # 2. Run alembic upgrade head as migrator
        migrator_url = parsed_url.set(
            username="migrator", password="migrator_secure_pass", database=test_db_name
        ).render_as_string(hide_password=False)
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", migrator_url.replace("%", "%%"))
        command.upgrade(alembic_cfg, "head")

        # 3. Connect as app_rw
        app_rw_url = parsed_url.set(
            username="app_rw", password="app_rw_secure_pass", database=test_db_name
        ).render_as_string(hide_password=False)
        app_rw_engine = create_engine(app_rw_url)
        AppRwSession = sessionmaker(bind=app_rw_engine, autocommit=False, autoflush=False)

        with AppRwSession() as session:
            # Seed minimal required customer, user, policy, coverage
            cust = Customer(
                id=uuid.uuid4(),
                customer_no="CUST-SMOKE-001",
                full_name="Smoke Test Customer",
                dob=date(1990, 1, 1),
                email="smoke.customer@example.com",
                phone="+15550099",
                address="Smoke Test Way",
                state_region="CA",
            )
            user = User(
                id=uuid.uuid4(),
                email="smoke.customer@example.com",
                password_hash=hash_password("SmokePass123456!"),
                role=UserRole.CUSTOMER,
                customer_id=cust.id,
                is_active=True,
            )
            policy = Policy(
                id=uuid.uuid4(),
                policy_no="POL-SMOKE-001",
                customer_id=cust.id,
                product_type=ProductType.VEHICLE,
                start_date=date.today() - timedelta(days=30),
                end_date=date.today() + timedelta(days=335),
                premium=Decimal("20000.00"),
                sum_insured=Decimal("500000.00"),
                status=PolicyStatus.ACTIVE,
            )
            cov = Coverage(
                id=uuid.uuid4(),
                policy_id=policy.id,
                claim_category="COLLISION",
                limit_scope=LimitScope.PER_CLAIM,
                limit_amount=Decimal("300000.00"),
                consumed_amount=Decimal("0.00"),
                deductible=Decimal("2000.00"),
                copay_percent=Decimal("0.00"),
                waiting_period_days=0,
                is_excluded=False,
                limit_exceeded_behavior="CAP",
            )
            session.add_all([cust, user, policy, cov])
            session.commit()

            # Register claim as app_rw
            from app.audit.verifier import verify_chain
            from app.services.claim_service import ClaimService

            claim_service = ClaimService(session)
            claim = claim_service.register_claim(
                policy_id=policy.id,
                claim_category="COLLISION",
                incident_date=date.today() - timedelta(days=2),
                incident_location="Smoke Test Blvd",
                incident_description="Staging smoke test registering claim as app_rw",
                requested_amount=Decimal("25000.00"),
                registered_by_id=user.id,
                registered_by_role="customer",
            )
            session.commit()

            assert claim.id is not None
            assert claim.claim_ref.startswith("CLM-")
            assert claim.status.value == "REGISTERED"

            # Verify audit chain written by app_rw is cryptographically intact
            chain_key = f"claim:{claim.id}"
            res = verify_chain(session, chain_key, record_run=False)
            assert res.is_valid is True
            assert res.total_events >= 1
            assert res.first_broken_seq is None

        app_rw_engine.dispose()
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                text(f"DROP DATABASE IF EXISTS {test_db_name} WITH (FORCE);")
                if conn.dialect.server_version_info >= (13,)
                else text(f"DROP DATABASE IF EXISTS {test_db_name};")
            )
        admin_engine.dispose()

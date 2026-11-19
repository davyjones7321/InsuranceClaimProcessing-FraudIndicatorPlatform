"""Deterministic seed script for the insurance claims platform.

Refuses execution in production environment.
Creates users for all roles, 25 customers, policies across HEALTH/VEHICLE/PROPERTY,
coverages, exclusions, insured items, and required document configurations.
"""

import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.config import settings
from app.db.models import (
    Coverage,
    Customer,
    Exclusion,
    Policy,
    PolicyInsuredItem,
    RequiredDocumentType,
    User,
)
from app.db.session import SessionLocal
from app.domain.enums import LimitScope, PolicyStatus, ProductType, UserRole
from app.security.password import hash_password

# Fixed namespace UUID for deterministic IDs
SEED_NAMESPACE = uuid.UUID("11111111-2222-3333-4444-555555555555")


def seed_uuid(name: str) -> uuid.UUID:
    """Generate a deterministic UUID from a unique seed key."""
    return uuid.uuid5(SEED_NAMESPACE, name)


def seed_database(db: Session, default_password: str = "DevPassword123!") -> None:
    """Populate database with deterministic master data."""
    if settings.ENV == "production":
        raise RuntimeError("Seed script must not run in production environment.")

    # 1. Required Document Types
    doc_configs = [
        # Health
        (ProductType.HEALTH, None, "DOC_MED_BILL", "Medical Bill", True),
        (ProductType.HEALTH, None, "DOC_DISCHARGE", "Discharge Summary", True),
        (ProductType.HEALTH, None, "DOC_PRESCRIPTION", "Doctor Prescription", True),
        (ProductType.HEALTH, None, "DOC_DIAGNOSTIC", "Diagnostic Test Report", False),
        # Vehicle
        (ProductType.VEHICLE, None, "DOC_DRIVING_LICENSE", "Driving License", True),
        (ProductType.VEHICLE, None, "DOC_REPAIR_ESTIMATE", "Repair Estimate Invoice", True),
        (ProductType.VEHICLE, None, "DOC_INCIDENT_PHOTO", "Accident Scene Photos", True),
        (ProductType.VEHICLE, None, "DOC_POLICE_REPORT", "Police FIR", False),
        # Property
        (ProductType.PROPERTY, None, "DOC_DAMAGE_PHOTO", "Damage Photos", True),
        (ProductType.PROPERTY, None, "DOC_REPAIR_INVOICE", "Repair Quotation / Invoice", True),
        (ProductType.PROPERTY, None, "DOC_OWNERSHIP_PROOF", "Proof of Ownership", True),
        (
            ProductType.PROPERTY,
            None,
            "DOC_FIRE_DEPT_REPORT",
            "Fire Department Incident Report",
            False,
        ),
    ]

    for p_type, cat, code, name, mandatory in doc_configs:
        existing = db.scalars(
            select(RequiredDocumentType).where(
                RequiredDocumentType.product_type == p_type,
                RequiredDocumentType.claim_category == cat,
                RequiredDocumentType.doc_code == code,
            )
        ).first()
        if not existing:
            doc_id = seed_uuid(f"doc_{p_type.value}_{cat}_{code}")
            db.add(
                RequiredDocumentType(
                    id=doc_id,
                    product_type=p_type,
                    claim_category=cat,
                    doc_code=code,
                    name=name,
                    is_mandatory=mandatory,
                )
            )

    # 2. Customers (25 deterministic records)
    regions = ["MH", "KA", "DL", "TN", "TS", "WB", "GJ"]
    customers: list[Customer] = []
    base_dob = date(1985, 1, 1)

    for i in range(1, 26):
        c_no = f"CUST-{10000 + i}"
        cust_id = seed_uuid(f"customer_{c_no}")
        existing_c = db.get(Customer, cust_id)
        if not existing_c:
            cust = Customer(
                id=cust_id,
                customer_no=c_no,
                full_name=f"Customer Name {i}",
                dob=base_dob + timedelta(days=i * 200),
                email=f"customer{i}@example.com",
                phone=f"+919876543{i:03d}",
                address=f"Street #{i}, Tech City Block {i % 5 + 1}",
                state_region=regions[i % len(regions)],
                source_system="SEED_V1",
                external_ref=f"EXT-CUST-{i:04d}",
            )
            db.add(cust)
            customers.append(cust)
        else:
            customers.append(existing_c)

    db.flush()

    # 3. Users for all roles
    hashed_pwd = hash_password(default_password)
    role_specs = [
        ("admin@example.com", UserRole.ADMIN, None),
        ("manager@example.com", UserRole.MANAGER, None),
        ("investigator@example.com", UserRole.INVESTIGATOR, None),
        ("assessor@example.com", UserRole.ASSESSOR, None),
        ("staff@example.com", UserRole.STAFF, None),
        ("customer1@example.com", UserRole.CUSTOMER, customers[0].id),
        ("customer2@example.com", UserRole.CUSTOMER, customers[1].id),
    ]

    for email, role, c_id in role_specs:
        existing_u = db.scalars(select(User).where(User.email == email)).first()
        if not existing_u:
            user_id = seed_uuid(f"user_{email}")
            db.add(
                User(
                    id=user_id,
                    email=email,
                    password_hash=hashed_pwd,
                    role=role,
                    customer_id=c_id,
                    is_active=True,
                )
            )

    db.flush()

    # 4. Policies across HEALTH, VEHICLE, PROPERTY (including ACTIVE, LAPSED, CANCELLED, EXPIRED)
    policy_templates = [
        # (suffix, product_type, status, days_ago_start, duration_days, premium, sum_insured)
        (
            "POL-HLT-001",
            ProductType.HEALTH,
            PolicyStatus.ACTIVE,
            60,
            365,
            Decimal("15000.00"),
            Decimal("500000.00"),
        ),
        (
            "POL-HLT-002",
            ProductType.HEALTH,
            PolicyStatus.ACTIVE,
            120,
            365,
            Decimal("22000.00"),
            Decimal("1000000.00"),
        ),
        (
            "POL-HLT-003",
            ProductType.HEALTH,
            PolicyStatus.LAPSED,
            400,
            365,
            Decimal("12000.00"),
            Decimal("300000.00"),
        ),
        (
            "POL-HLT-004",
            ProductType.HEALTH,
            PolicyStatus.CANCELLED,
            90,
            365,
            Decimal("18000.00"),
            Decimal("500000.00"),
        ),
        (
            "POL-HLT-005",
            ProductType.HEALTH,
            PolicyStatus.EXPIRED,
            450,
            365,
            Decimal("15000.00"),
            Decimal("500000.00"),
        ),
        (
            "POL-VEH-001",
            ProductType.VEHICLE,
            PolicyStatus.ACTIVE,
            30,
            365,
            Decimal("12500.00"),
            Decimal("800000.00"),
        ),
        (
            "POL-VEH-002",
            ProductType.VEHICLE,
            PolicyStatus.ACTIVE,
            180,
            365,
            Decimal("19000.00"),
            Decimal("1200000.00"),
        ),
        (
            "POL-VEH-003",
            ProductType.VEHICLE,
            PolicyStatus.LAPSED,
            500,
            365,
            Decimal("8000.00"),
            Decimal("400000.00"),
        ),
        (
            "POL-VEH-004",
            ProductType.VEHICLE,
            PolicyStatus.EXPIRED,
            400,
            365,
            Decimal("11000.00"),
            Decimal("600000.00"),
        ),
        (
            "POL-PRP-001",
            ProductType.PROPERTY,
            PolicyStatus.ACTIVE,
            90,
            730,
            Decimal("25000.00"),
            Decimal("5000000.00"),
        ),
        (
            "POL-PRP-002",
            ProductType.PROPERTY,
            PolicyStatus.ACTIVE,
            200,
            365,
            Decimal("18000.00"),
            Decimal("3000000.00"),
        ),
        (
            "POL-PRP-003",
            ProductType.PROPERTY,
            PolicyStatus.CANCELLED,
            40,
            365,
            Decimal("30000.00"),
            Decimal("6000000.00"),
        ),
        (
            "POL-PRP-004",
            ProductType.PROPERTY,
            PolicyStatus.EXPIRED,
            800,
            730,
            Decimal("45000.00"),
            Decimal("8000000.00"),
        ),
    ]

    today = date(2026, 11, 1)

    for idx, (p_no, p_type, p_status, days_start, duration, prem, sum_ins) in enumerate(
        policy_templates
    ):
        cust = customers[idx % len(customers)]
        existing_p = db.scalars(select(Policy).where(Policy.policy_no == p_no)).first()
        if existing_p:
            continue

        p_id = seed_uuid(f"policy_{p_no}")
        start_d = today - timedelta(days=days_start)
        end_d = start_d + timedelta(days=duration)

        policy = Policy(
            id=p_id,
            policy_no=p_no,
            customer_id=cust.id,
            product_type=p_type,
            start_date=start_d,
            end_date=end_d,
            premium=prem,
            sum_insured=sum_ins,
            status=p_status,
            source_system="SEED_V1",
            external_ref=f"EXT-{p_no}",
        )
        db.add(policy)

        # 5. Add coverages per product type
        if p_type == ProductType.HEALTH:
            db.add(
                Coverage(
                    id=seed_uuid(f"cov_{p_no}_hosp"),
                    policy_id=p_id,
                    claim_category="HOSPITALIZATION",
                    limit_scope=LimitScope.PER_POLICY_PERIOD,
                    limit_amount=Decimal("400000.00"),
                    consumed_amount=Decimal("0.00"),
                    deductible=Decimal("2000.00"),
                    copay_percent=Decimal("10.00"),
                    waiting_period_days=30,
                    is_excluded=False,
                )
            )
            db.add(
                Coverage(
                    id=seed_uuid(f"cov_{p_no}_surg"),
                    policy_id=p_id,
                    claim_category="SURGERY",
                    limit_scope=LimitScope.PER_POLICY_PERIOD,
                    limit_amount=Decimal("250000.00"),
                    consumed_amount=Decimal("0.00"),
                    deductible=Decimal("5000.00"),
                    copay_percent=Decimal("10.00"),
                    waiting_period_days=90,
                    is_excluded=False,
                )
            )
            db.add(
                Exclusion(
                    id=seed_uuid(f"excl_{p_no}_preex"),
                    policy_id=p_id,
                    exclusion_code="EXCL_PRE_EXISTING",
                    description="Pre-existing medical conditions not covered during initial 24 months",
                )
            )
            db.add(
                PolicyInsuredItem(
                    id=seed_uuid(f"item_{p_no}_primary"),
                    policy_id=p_id,
                    item_type="MEMBER",
                    identifier=f"MEM-{cust.customer_no}-01",
                    details={"relation": "SELF", "name": cust.full_name},
                )
            )

        elif p_type == ProductType.VEHICLE:
            db.add(
                Coverage(
                    id=seed_uuid(f"cov_{p_no}_acc"),
                    policy_id=p_id,
                    claim_category="ACCIDENTAL_DAMAGE",
                    limit_scope=LimitScope.PER_POLICY_PERIOD,
                    limit_amount=Decimal("500000.00"),
                    consumed_amount=Decimal("0.00"),
                    deductible=Decimal("1500.00"),
                    copay_percent=Decimal("0.00"),
                    waiting_period_days=0,
                    is_excluded=False,
                )
            )
            db.add(
                Coverage(
                    id=seed_uuid(f"cov_{p_no}_tp"),
                    policy_id=p_id,
                    claim_category="THIRD_PARTY",
                    limit_scope=LimitScope.PER_POLICY_PERIOD,
                    limit_amount=Decimal("1000000.00"),
                    consumed_amount=Decimal("0.00"),
                    deductible=Decimal("0.00"),
                    copay_percent=Decimal("0.00"),
                    waiting_period_days=0,
                    is_excluded=False,
                )
            )
            db.add(
                Exclusion(
                    id=seed_uuid(f"excl_{p_no}_racing"),
                    policy_id=p_id,
                    exclusion_code="EXCL_MOTOR_SPORTS",
                    description="Damages resulting from competitive motor sports or illegal racing",
                )
            )
            db.add(
                PolicyInsuredItem(
                    id=seed_uuid(f"item_{p_no}_car"),
                    policy_id=p_id,
                    item_type="VEHICLE",
                    identifier=f"MH-12-AB-{1000 + idx}",
                    details={"make": "Toyota", "model": "Corolla", "vin": f"VIN{p_no}XYZ"},
                )
            )

        elif p_type == ProductType.PROPERTY:
            db.add(
                Coverage(
                    id=seed_uuid(f"cov_{p_no}_fire"),
                    policy_id=p_id,
                    claim_category="FIRE",
                    limit_scope=LimitScope.PER_POLICY_PERIOD,
                    limit_amount=Decimal("3000000.00"),
                    consumed_amount=Decimal("0.00"),
                    deductible=Decimal("10000.00"),
                    copay_percent=Decimal("5.00"),
                    waiting_period_days=0,
                    is_excluded=False,
                )
            )
            db.add(
                Exclusion(
                    id=seed_uuid(f"excl_{p_no}_war"),
                    policy_id=p_id,
                    exclusion_code="EXCL_WAR_PERIL",
                    description="Loss or damage directly caused by acts of foreign hostility or war",
                )
            )
            db.add(
                PolicyInsuredItem(
                    id=seed_uuid(f"item_{p_no}_prop"),
                    policy_id=p_id,
                    item_type="PROPERTY",
                    identifier=f"PROP-SURVEY-{2000 + idx}",
                    details={"address": cust.address, "city": "Metropolis"},
                )
            )

    db.flush()

    # Log audit entry on system chain
    admin_user = db.scalars(select(User).where(User.role == UserRole.ADMIN)).first()
    actor_id = admin_user.id if admin_user else seed_uuid("system_actor")
    write_audit_log(
        db=db,
        action="SEED_DATABASE",
        entity_type="MasterData",
        entity_id=str(seed_uuid("seed_v1_run")),
        actor_role=UserRole.ADMIN.value,
        actor_id=actor_id,
        event_data={
            "customers_count": len(customers),
            "policies_count": len(policy_templates),
            "doc_types_count": len(doc_configs),
        },
    )

    db.commit()


def main() -> None:
    if settings.ENV == "production":
        print("Error: Refusing to run seed script in production environment.", file=sys.stderr)
        sys.exit(1)

    db = SessionLocal()
    try:
        seed_database(db)
        print("Master data successfully seeded with deterministic data.")
    except Exception as exc:
        db.rollback()
        print(f"Error seeding database: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

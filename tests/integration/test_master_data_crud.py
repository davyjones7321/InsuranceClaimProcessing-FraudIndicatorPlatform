"""Integration tests for Master Data CRUD and Role-Based Access Control."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import User
from app.domain.enums import UserRole
from app.security.password import hash_password
from app.security.session import create_session


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def test_users(db_session: Session) -> dict[str, User]:
    """Create test users for all roles."""
    users = {}
    roles = [
        UserRole.ADMIN,
        UserRole.MANAGER,
        UserRole.INVESTIGATOR,
        UserRole.ASSESSOR,
        UserRole.STAFF,
        UserRole.CUSTOMER,
    ]
    for r in roles:
        u = User(
            email=f"{r.value}@platform.test",
            password_hash=hash_password("MasterPassword123!"),
            role=r,
            is_active=True,
        )
        db_session.add(u)
        users[r.value] = u
    db_session.commit()
    for u in users.values():
        db_session.refresh(u)
    return users


def test_customer_crud_and_rbac(
    client: TestClient, db_session: Session, test_users: dict[str, User]
) -> None:
    admin_hdr = auth_headers(db_session, test_users["admin"])
    staff_hdr = auth_headers(db_session, test_users["staff"])
    cust_hdr = auth_headers(db_session, test_users["customer"])

    # 1. Unauthenticated creation fails
    res = client.post(
        "/api/customers",
        json={
            "customer_no": "CUST-CRUD-001",
            "full_name": "Jane Doe",
            "dob": "1990-05-15",
            "email": "jane.doe@example.com",
            "phone": "+1234567890",
            "address": "123 Main St",
            "state_region": "NY",
        },
    )
    assert res.status_code == 401

    # 2. Staff cannot create customer (Admin only)
    res = client.post(
        "/api/customers",
        headers=staff_hdr,
        json={
            "customer_no": "CUST-CRUD-001",
            "full_name": "Jane Doe",
            "dob": "1990-05-15",
            "email": "jane.doe@example.com",
            "phone": "+1234567890",
            "address": "123 Main St",
            "state_region": "NY",
        },
    )
    assert res.status_code == 403

    # Customer also cannot create customer (Admin only)
    res_cust = client.post(
        "/api/customers",
        headers=cust_hdr,
        json={
            "customer_no": "CUST-CRUD-001",
            "full_name": "Jane Doe",
            "dob": "1990-05-15",
            "email": "jane.doe@example.com",
            "phone": "+1234567890",
            "address": "123 Main St",
            "state_region": "NY",
        },
    )
    assert res_cust.status_code == 403

    # 3. Admin creates customer
    res = client.post(
        "/api/customers",
        headers=admin_hdr,
        json={
            "customer_no": "CUST-CRUD-001",
            "full_name": "Jane Doe",
            "dob": "1990-05-15",
            "email": "jane.doe@example.com",
            "phone": "+1234567890",
            "address": "123 Main St",
            "state_region": "NY",
        },
    )
    assert res.status_code == 201
    created_cust = res.json()
    cust_id = created_cust["id"]
    assert created_cust["customer_no"] == "CUST-CRUD-001"

    # 4. Duplicate customer_no fails
    res = client.post(
        "/api/customers",
        headers=admin_hdr,
        json={
            "customer_no": "CUST-CRUD-001",
            "full_name": "Duplicate Doe",
            "dob": "1990-05-15",
            "email": "dup@example.com",
            "phone": "+1234567890",
            "address": "123 Main St",
            "state_region": "NY",
        },
    )
    assert res.status_code == 409

    # 5. Staff lists customers
    res = client.get("/api/customers?search=Jane", headers=staff_hdr)
    assert res.status_code == 200
    assert len(res.json()) == 1

    # 6. Admin updates customer
    res = client.put(
        f"/api/customers/{cust_id}",
        headers=admin_hdr,
        json={"phone": "+9876543210", "state_region": "CA"},
    )
    assert res.status_code == 200
    assert res.json()["phone"] == "+9876543210"
    assert res.json()["state_region"] == "CA"


def test_policy_and_coverage_crud(
    client: TestClient, db_session: Session, test_users: dict[str, User]
) -> None:
    admin_hdr = auth_headers(db_session, test_users["admin"])
    staff_hdr = auth_headers(db_session, test_users["staff"])

    # First, create a customer
    res = client.post(
        "/api/customers",
        headers=admin_hdr,
        json={
            "customer_no": "CUST-POL-001",
            "full_name": "Bob Smith",
            "dob": "1988-03-20",
            "email": "bob.smith@example.com",
            "phone": "+1122334455",
            "address": "456 Elm St",
            "state_region": "TX",
        },
    )
    assert res.status_code == 201
    cust_id = res.json()["id"]

    # 1. Admin creates policy with nested coverages, exclusions, and items
    policy_payload = {
        "policy_no": "POL-TEST-001",
        "customer_id": cust_id,
        "product_type": "HEALTH",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "premium": "12000.00",
        "sum_insured": "500000.00",
        "status": "ACTIVE",
        "coverages": [
            {
                "claim_category": "HOSPITALIZATION",
                "limit_scope": "PER_POLICY_PERIOD",
                "limit_amount": "400000.00",
                "deductible": "2000.00",
                "copay_percent": "10.00",
                "waiting_period_days": 30,
            }
        ],
        "exclusions": [
            {
                "exclusion_code": "EXCL_COSMETIC",
                "description": "Elective cosmetic surgeries",
            }
        ],
        "insured_items": [
            {
                "item_type": "MEMBER",
                "identifier": "MEM-BOB-01",
                "details": {"relation": "SELF"},
            }
        ],
    }
    res = client.post("/api/policies", headers=admin_hdr, json=policy_payload)
    assert res.status_code == 201
    created_policy = res.json()
    pol_id = created_policy["id"]
    assert len(created_policy["coverages"]) == 1
    assert len(created_policy["exclusions"]) == 1
    assert len(created_policy["insured_items"]) == 1

    # 2. Staff views policy
    res = client.get(f"/api/policies/{pol_id}", headers=staff_hdr)
    assert res.status_code == 200
    assert res.json()["policy_no"] == "POL-TEST-001"

    # 3. Add coverage via standalone coverage route
    res = client.post(
        f"/api/policies/{pol_id}/coverages",
        headers=admin_hdr,
        json={
            "claim_category": "SURGERY",
            "limit_scope": "PER_POLICY_PERIOD",
            "limit_amount": "200000.00",
            "deductible": "5000.00",
            "copay_percent": "15.00",
        },
    )
    assert res.status_code == 201
    new_cov_id = res.json()["id"]

    # 4. Update coverage
    res = client.put(
        f"/api/coverages/{new_cov_id}",
        headers=admin_hdr,
        json={"limit_amount": "250000.00", "copay_percent": "10.00"},
    )
    assert res.status_code == 200
    assert res.json()["limit_amount"] == "250000.00"

    # 5. List coverages for policy
    res = client.get(f"/api/policies/{pol_id}/coverages", headers=staff_hdr)
    assert res.status_code == 200
    assert len(res.json()) == 2

    # 6. Add and delete exclusion
    res = client.post(
        f"/api/policies/{pol_id}/exclusions",
        headers=admin_hdr,
        json={
            "exclusion_code": "EXCL_DENTAL",
            "description": "Routine dental care",
        },
    )
    assert res.status_code == 201
    excl_id = res.json()["id"]

    res = client.delete(f"/api/exclusions/{excl_id}", headers=admin_hdr)
    assert res.status_code == 204

    # 7. Update policy status
    res = client.put(
        f"/api/policies/{pol_id}",
        headers=admin_hdr,
        json={"status": "LAPSED"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "LAPSED"


def test_required_document_types_crud(
    client: TestClient, db_session: Session, test_users: dict[str, User]
) -> None:
    admin_hdr = auth_headers(db_session, test_users["admin"])
    cust_hdr = auth_headers(db_session, test_users["customer"])

    # 1. Admin creates required document type
    res = client.post(
        "/api/required-documents",
        headers=admin_hdr,
        json={
            "product_type": "HEALTH",
            "claim_category": "HOSPITALIZATION",
            "doc_code": "DOC_HOSP_BILL",
            "name": "Hospital Final Itemized Bill",
            "is_mandatory": True,
        },
    )
    assert res.status_code == 201
    doc_id = res.json()["id"]

    # 2. Customer can list required documents
    res = client.get("/api/required-documents?product_type=HEALTH", headers=cust_hdr)
    assert res.status_code == 200
    items = res.json()
    assert any(d["doc_code"] == "DOC_HOSP_BILL" for d in items)

    # 3. Admin updates document type
    res = client.put(
        f"/api/required-documents/{doc_id}",
        headers=admin_hdr,
        json={"name": "Hospital Itemized Final Bill (Updated)", "is_mandatory": False},
    )
    assert res.status_code == 200
    assert res.json()["is_mandatory"] is False

    # 4. Admin deletes document type
    res = client.delete(f"/api/required-documents/{doc_id}", headers=admin_hdr)
    assert res.status_code == 204

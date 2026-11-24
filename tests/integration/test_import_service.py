"""Integration tests for Master Data Import service and API endpoints."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Customer, Policy, User
from app.domain.enums import UserRole
from app.security.password import hash_password
from app.security.session import create_session


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


@pytest.fixture
def admin_user(db_session: Session) -> User:
    admin = User(
        email="import_admin@test.com",
        password_hash=hash_password("AdminSecurePass123!"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


def test_json_batch_import_dry_run_and_commit(
    client: TestClient, db_session: Session, admin_user: User
) -> None:
    admin_hdr = auth_headers(db_session, admin_user)

    batch_payload = {
        "customers": [
            {
                "customer_no": "CUST-JSON-001",
                "full_name": "John JSON",
                "dob": "1985-06-15",
                "email": "john.json@example.com",
                "phone": "+1987654321",
                "address": "100 JSON Boulevard",
                "state_region": "CA",
            }
        ],
        "policies": [],
        "required_documents": [
            {
                "product_type": "HEALTH",
                "claim_category": None,
                "doc_code": "DOC_JSON_REQ",
                "name": "JSON Required Proof",
                "is_mandatory": True,
            }
        ],
    }

    # 1. Dry run should report processed/created counts but commit nothing to DB
    res_dry = client.post(
        "/api/master-data/import/json?dry_run=true",
        headers=admin_hdr,
        json=batch_payload,
    )
    assert res_dry.status_code == 200
    summary_dry = res_dry.json()
    assert summary_dry["dry_run"] is True
    assert summary_dry["customers_processed"] == 1
    assert summary_dry["items_created"] == 2
    assert summary_dry["errors"] == []

    # Verify database has not been touched
    cust_check = db_session.scalars(
        select(Customer).where(Customer.customer_no == "CUST-JSON-001")
    ).first()
    assert cust_check is None

    # 2. Real import (dry_run=false)
    res_real = client.post(
        "/api/master-data/import/json?dry_run=false",
        headers=admin_hdr,
        json=batch_payload,
    )
    assert res_real.status_code == 200
    summary_real = res_real.json()
    assert summary_real["dry_run"] is False
    assert summary_real["items_created"] == 2

    # Verify entity exists
    cust_created = db_session.scalars(
        select(Customer).where(Customer.customer_no == "CUST-JSON-001")
    ).first()
    assert cust_created is not None
    assert cust_created.full_name == "John JSON"

    # 3. Idempotent rerun with updated name
    batch_payload["customers"][0]["full_name"] = "John JSON Updated"
    res_rerun = client.post(
        "/api/master-data/import/json?dry_run=false",
        headers=admin_hdr,
        json=batch_payload,
    )
    assert res_rerun.status_code == 200
    summary_rerun = res_rerun.json()
    assert summary_rerun["items_updated"] >= 1


def test_csv_import_customers_and_policies(
    client: TestClient, db_session: Session, admin_user: User
) -> None:
    admin_hdr = auth_headers(db_session, admin_user)

    # 1. Import customers via CSV
    customers_csv = (
        "customer_no,full_name,dob,email,phone,address,state_region\n"
        "CUST-CSV-001,Alice CSV,1992-04-10,alice.csv@example.com,+15550011,10 CSV Lane,WA\n"
        "CUST-CSV-002,Bob CSV,1989-11-25,bob.csv@example.com,+15550022,20 CSV Street,OR\n"
    )

    res_cust = client.post(
        "/api/master-data/import/csv?entity_type=customers&dry_run=false",
        headers={**admin_hdr, "Content-Type": "text/plain"},
        content=customers_csv,
    )
    assert res_cust.status_code == 200
    cust_summary = res_cust.json()
    assert cust_summary["customers_processed"] == 2
    assert cust_summary["items_created"] == 2
    assert cust_summary["errors"] == []

    # 2. Import policies via CSV referencing imported customer
    policies_csv = (
        "policy_no,customer_no,product_type,start_date,end_date,premium,sum_insured,status\n"
        "POL-CSV-001,CUST-CSV-001,HEALTH,2026-01-01,2026-12-31,15000.00,500000.00,ACTIVE\n"
    )

    res_pol = client.post(
        "/api/master-data/import/csv?entity_type=policies&dry_run=false",
        headers={**admin_hdr, "Content-Type": "text/plain"},
        content=policies_csv,
    )
    assert res_pol.status_code == 200
    pol_summary = res_pol.json()
    assert pol_summary["policies_processed"] == 1
    assert pol_summary["items_created"] == 1
    assert pol_summary["errors"] == []

    # Verify policy in DB
    pol = db_session.scalars(select(Policy).where(Policy.policy_no == "POL-CSV-001")).first()
    assert pol is not None
    assert str(pol.sum_insured) == "500000.00"

    # 3. Import coverages via CSV referencing imported policy
    coverages_csv = (
        "policy_no,claim_category,limit_scope,limit_amount,deductible,copay_percent,waiting_period_days,is_excluded\n"
        "POL-CSV-001,HOSPITALIZATION,PER_POLICY_PERIOD,400000.00,2000.00,10.00,30,false\n"
    )

    res_cov = client.post(
        "/api/master-data/import/csv?entity_type=coverages&dry_run=false",
        headers={**admin_hdr, "Content-Type": "text/plain"},
        content=coverages_csv,
    )
    assert res_cov.status_code == 200
    cov_summary = res_cov.json()
    assert cov_summary["coverages_processed"] == 1
    assert cov_summary["items_created"] == 1

    # 4. CSV Import error resilience: invalid customer reference
    bad_policies_csv = (
        "policy_no,customer_no,product_type,start_date,end_date,premium,sum_insured,status\n"
        "POL-CSV-BAD,NON_EXISTENT_CUST,HEALTH,2026-01-01,2026-12-31,1000.00,10000.00,ACTIVE\n"
    )
    res_bad = client.post(
        "/api/master-data/import/csv?entity_type=policies&dry_run=false",
        headers={**admin_hdr, "Content-Type": "text/plain"},
        content=bad_policies_csv,
    )
    assert res_bad.status_code == 200
    bad_summary = res_bad.json()
    assert len(bad_summary["errors"]) == 1
    assert "NON_EXISTENT_CUST" in bad_summary["errors"][0]

"""Integration tests for Manager Analytics Dashboard and CSV Export."""

import csv
import io

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User
from app.domain.enums import UserRole
from app.security.session import create_session
from scripts.seed import seed_database


def auth_headers(db: Session, user: User) -> dict[str, str]:
    session_id = create_session(db, user)
    db.commit()
    return {"Authorization": f"Bearer {session_id}"}


def test_manager_analytics_dashboard_metrics(client: TestClient, db_session: Session) -> None:
    """Verify aggregated dashboard metrics computed via SQL against seeded dataset."""
    seed_database(db_session)

    manager = db_session.scalars(select(User).where(User.role == UserRole.MANAGER)).first()
    assert manager is not None
    headers = auth_headers(db_session, manager)

    resp = client.get("/api/analytics/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # 1. Volume by Product
    vol_prod = {item["product_type"]: item["count"] for item in data["volume_by_product"]}
    assert "HEALTH" in vol_prod and vol_prod["HEALTH"] > 0
    assert "VEHICLE" in vol_prod and vol_prod["VEHICLE"] > 0
    assert "PROPERTY" in vol_prod and vol_prod["PROPERTY"] > 0

    # 2. Volume by Status
    vol_stat = {item["status"]: item["count"] for item in data["volume_by_status"]}
    assert vol_stat.get("APPROVED", 0) == 35
    assert vol_stat.get("REJECTED", 0) == 20
    assert vol_stat.get("REGISTERED", 0) == 20

    # 3. Volume Over Time
    assert len(data["volume_over_time"]) > 0

    # 4. Processing Time
    proc_time = data["processing_time"]
    assert proc_time["overall_avg_hours"] >= 0.0
    assert proc_time["overall_median_hours"] >= 0.0

    # 5. Financial Metrics
    fin = data["financial_metrics"]
    assert float(fin["total_requested"]) > 0.0
    assert float(fin["total_approved"]) > 0.0
    assert "HEALTH" in fin["by_product"]
    assert float(fin["by_product"]["HEALTH"]["requested"]) > 0.0

    # 6. Risk Metrics
    risk = data["risk_metrics"]
    assert risk["total_evaluations"] > 0
    assert risk["flagged_evaluations"] > 0
    assert risk["investigation_rate_percent"] > 0.0
    assert "LOW" in risk["category_distribution"]
    assert "MEDIUM" in risk["category_distribution"]

    # 7. Investigation Metrics
    inv = data["investigation_metrics"]
    assert inv["open_count"] >= 0
    assert inv["completed_count"] >= 0

    # 8. Backlog Metrics
    backlog = data["backlog_metrics"]
    assert backlog["total_active_backlog"] > 0
    assert "<7d" in backlog["by_age"]


def test_analytics_dashboard_filtering(client: TestClient, db_session: Session) -> None:
    """Verify that query filters (product_type, status) isolate matching subsets."""
    seed_database(db_session)

    manager = db_session.scalars(select(User).where(User.role == UserRole.MANAGER)).first()
    assert manager is not None
    headers = auth_headers(db_session, manager)

    # Filter by ProductType HEALTH
    resp = client.get("/api/analytics/dashboard?product_type=HEALTH", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["volume_by_product"]) == 1
    assert data["volume_by_product"][0]["product_type"] == "HEALTH"

    # Filter by Status APPROVED
    resp_stat = client.get("/api/analytics/dashboard?status=APPROVED", headers=headers)
    assert resp_stat.status_code == 200
    data_stat = resp_stat.json()
    assert len(data_stat["volume_by_status"]) == 1
    assert data_stat["volume_by_status"][0]["status"] == "APPROVED"


def test_analytics_csv_export(client: TestClient, db_session: Session) -> None:
    """Verify CSV export endpoint streams valid comma-separated rows."""
    seed_database(db_session)

    manager = db_session.scalars(select(User).where(User.role == UserRole.MANAGER)).first()
    assert manager is not None
    headers = auth_headers(db_session, manager)

    resp = client.get("/api/analytics/export", headers=headers)
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert (
        'attachment; filename="claims_analytics_export.csv"' in resp.headers["content-disposition"]
    )

    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)
    assert len(rows) > 200  # header + 230 seeded claims

    header = rows[0]
    assert "Claim Reference" in header
    assert "Customer Name" in header
    assert "Requested Amount" in header
    assert "Approved Amount" in header


def test_analytics_rbac_restrictions(client: TestClient, db_session: Session) -> None:
    """Only Manager and Admin roles can access analytics endpoints."""
    seed_database(db_session)

    customer = db_session.scalars(select(User).where(User.role == UserRole.CUSTOMER)).first()
    staff = db_session.scalars(select(User).where(User.role == UserRole.STAFF)).first()
    assessor = db_session.scalars(select(User).where(User.role == UserRole.ASSESSOR)).first()
    investigator = db_session.scalars(
        select(User).where(User.role == UserRole.INVESTIGATOR)
    ).first()
    admin = db_session.scalars(select(User).where(User.role == UserRole.ADMIN)).first()

    assert customer and staff and assessor and investigator and admin

    for user in [customer, staff, assessor, investigator]:
        h = auth_headers(db_session, user)
        resp = client.get("/api/analytics/dashboard", headers=h)
        assert resp.status_code == 403, f"Expected 403 for role {user.role}, got {resp.status_code}"

    # Admin access succeeds
    admin_h = auth_headers(db_session, admin)
    assert client.get("/api/analytics/dashboard", headers=admin_h).status_code == 200

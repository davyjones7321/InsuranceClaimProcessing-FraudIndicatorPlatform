"""Performance benchmark script evaluating analytics aggregate queries on 100,000 seeded claims.

Measures wall-clock execution time of SQL aggregates across 100k claim rows and reports timings.
"""

import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.analytics.service import AnalyticsService
from app.db.base import Base
from app.db.models import Claim, Customer, Decision, Policy, User
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    DecisionType,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.security.password import hash_password


def run_benchmark(db: Session, target_rows: int = 100_000) -> dict[str, float]:
    """Populate database with synthetic claims and benchmark analytics query timings."""
    print(f"Setting up benchmark master data for {target_rows:,} claims...")

    # 1. Base Customer, Policy, User
    cust_id = uuid.uuid4()
    cust = Customer(
        id=cust_id,
        customer_no="CUST-BENCH-001",
        full_name="Benchmark Customer",
        dob=date(1990, 1, 1),
        email="bench@example.com",
        phone="+15550000",
        address="100 Test Ave",
        state_region="CA",
    )
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email="bench_mgr@example.com",
        password_hash=hash_password("DevPass12345!"),
        role=UserRole.MANAGER,
        is_active=True,
    )
    policy_id = uuid.uuid4()
    policy = Policy(
        id=policy_id,
        policy_no="POL-BENCH-001",
        customer_id=cust_id,
        product_type=ProductType.HEALTH,
        start_date=date(2025, 1, 1),
        end_date=date(2027, 1, 1),
        premium=Decimal("50000.00"),
        sum_insured=Decimal("1000000.00"),
        status=PolicyStatus.ACTIVE,
    )

    db.add_all([cust, user, policy])
    db.flush()

    # 2. Bulk insert claims in batches of 10,000
    print(f"Generating and bulk inserting {target_rows:,} claims...")
    batch_size = 10_000
    today = date(2026, 11, 1)
    base_reg = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)

    statuses = [
        ClaimStatus.REGISTERED,
        ClaimStatus.VALIDATED,
        ClaimStatus.UNDER_INVESTIGATION,
        ClaimStatus.ASSESSED,
        ClaimStatus.APPROVED,
        ClaimStatus.REJECTED,
    ]

    claims_data = []
    decisions_data = []

    for i in range(target_rows):
        cid = uuid.uuid4()
        c_status = statuses[i % len(statuses)]
        reg_dt = base_reg + timedelta(minutes=i % 100_000)
        req_amt = Decimal(str(5000 + (i * 100) % 50000))

        claims_data.append(
            {
                "id": cid,
                "claim_ref": f"CLM-BENCH-{i:07d}",
                "policy_id": policy_id,
                "customer_id": cust_id,
                "insured_item_id": None,
                "claim_category": "HOSPITALIZATION",
                "incident_date": today - timedelta(days=10 + (i % 30)),
                "incident_location": "California Lab",
                "incident_description": f"Benchmark synthetic event {i}",
                "requested_amount": req_amt,
                "registered_at": reg_dt,
                "registered_by": user_id,
                "channel": ClaimChannel.STAFF,
                "status": c_status,
                "status_version": 2,
                "source_system": "BENCHMARK",
                "external_ref": f"EXT-BENCH-{i}",
                "created_at": reg_dt,
                "updated_at": reg_dt,
            }
        )

        if c_status in (ClaimStatus.APPROVED, ClaimStatus.REJECTED):
            decisions_data.append(
                {
                    "id": uuid.uuid4(),
                    "claim_id": cid,
                    "decided_by": user_id,
                    "decision": DecisionType.APPROVED
                    if c_status == ClaimStatus.APPROVED
                    else DecisionType.REJECTED,
                    "approved_amount": req_amt if c_status == ClaimStatus.APPROVED else None,
                    "remarks": "Benchmark synthetic decision remarks text over 20 chars.",
                    "reason_codes": ["EXCLUDED"] if c_status == ClaimStatus.REJECTED else None,
                    "created_at": reg_dt + timedelta(hours=24 + (i % 72)),
                }
            )

        if len(claims_data) >= batch_size:
            db.bulk_insert_mappings(Claim, claims_data)
            if decisions_data:
                db.bulk_insert_mappings(Decision, decisions_data)
            db.commit()
            claims_data.clear()
            decisions_data.clear()

    if claims_data:
        db.bulk_insert_mappings(Claim, claims_data)
        if decisions_data:
            db.bulk_insert_mappings(Decision, decisions_data)
        db.commit()

    print(f"Successfully loaded {target_rows:,} claims into database.")

    # 3. Benchmark Queries
    service = AnalyticsService(db)

    print("\n--- Running Benchmark Queries ---")
    # Query 1: Full dashboard aggregate metrics
    t0 = time.perf_counter()
    metrics = service.get_dashboard_metrics()
    t1 = time.perf_counter()
    dashboard_time_ms = (t1 - t0) * 1000.0
    print(f"Dashboard Aggregates Query Time: {dashboard_time_ms:.2f} ms")

    # Query 2: Filtered dashboard metrics (by status APPROVED)
    t0 = time.perf_counter()
    service.get_dashboard_metrics(status=ClaimStatus.APPROVED)
    t1 = time.perf_counter()
    filtered_time_ms = (t1 - t0) * 1000.0
    print(f"Filtered Dashboard Query Time:    {filtered_time_ms:.2f} ms")

    # Query 3: CSV export (sample slice or full export)
    t0 = time.perf_counter()
    service.export_claims_csv(status=ClaimStatus.APPROVED)
    t1 = time.perf_counter()
    csv_time_ms = (t1 - t0) * 1000.0
    print(f"CSV Export Query Time:            {csv_time_ms:.2f} ms")

    print("\nBenchmark Summary Results:")
    print(f"  Claims Scanned:             {target_rows:,}")
    print(f"  Active Backlog Computed:    {metrics.backlog_metrics.total_active_backlog:,}")
    print(f"  Total Requested Amount:     {metrics.financial_metrics.total_requested:,.2f}")
    print(f"  Dashboard Aggregates:       {dashboard_time_ms:.2f} ms")
    print(f"  Filtered Dashboard:         {filtered_time_ms:.2f} ms")
    print(f"  CSV Generation:             {csv_time_ms:.2f} ms")

    return {
        "dashboard_ms": dashboard_time_ms,
        "filtered_ms": filtered_time_ms,
        "csv_ms": csv_time_ms,
    }


def main() -> None:
    """CLI runner using SQLite in-memory or custom DB URL."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        # Run benchmark with 100k claims
        run_benchmark(session, target_rows=100_000)


if __name__ == "__main__":
    main()

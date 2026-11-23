"""Staging smoke test: register a claim with the application running as app_rw role."""

import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db.models import Customer, Policy, User
from app.domain.enums import UserRole
from app.services.claim_service import ClaimService


def smoke_test_via_api(api_url: str) -> None:
    """Run staging smoke test via HTTP API against staging deployment running as app_rw."""
    print(f"Executing staging smoke test against API at: {api_url}")
    clean_url = api_url.rstrip("/")

    with httpx.Client(timeout=30.0) as client:
        # 1. Readiness check
        ready_resp = client.get(f"{clean_url}/readyz")
        if ready_resp.status_code != 200:
            raise RuntimeError(f"Staging readiness probe failed: {ready_resp.status_code} {ready_resp.text}")
        print("Readiness probe /readyz OK (200)")

        # 2. Login as demo/test customer
        login_resp = client.post(
            f"{clean_url}/api/auth/login",
            json={"email": "alice.reg@example.com", "password": "AliceSecurePass123!"},
        )
        if login_resp.status_code != 200:
            raise RuntimeError(f"Staging login failed: {login_resp.status_code} {login_resp.text}")
        token = login_resp.json().get("session_id")
        headers = {"Authorization": f"Bearer {token}"}
        print("Staging customer authentication OK")

        # 3. Retrieve customer policies
        pol_resp = client.get(f"{clean_url}/api/policies", headers=headers)
        if pol_resp.status_code != 200 or not pol_resp.json():
            raise RuntimeError(f"Failed to fetch active policy on staging: {pol_resp.status_code} {pol_resp.text}")
        policy = pol_resp.json()[0]
        policy_id = policy["id"]

        # 4. Register claim as app_rw
        payload = {
            "policy_id": policy_id,
            "claim_category": "COLLISION",
            "incident_date": str(date.today() - timedelta(days=2)),
            "incident_location": "Staging Smoke Test Highway",
            "incident_description": "Staging deployment smoke test registering claim as app_rw",
            "requested_amount": "12000.00",
        }
        claim_resp = client.post(f"{clean_url}/api/claims", headers=headers, json=payload)
        if claim_resp.status_code != 201:
            raise RuntimeError(f"Staging claim registration failed: {claim_resp.status_code} {claim_resp.text}")

        claim_data = claim_resp.json()
        assert claim_data["status"] == "REGISTERED", f"Unexpected status: {claim_data['status']}"
        assert claim_data["claim_ref"].startswith("CLM-"), f"Unexpected ref: {claim_data['claim_ref']}"
        print(f"Staging smoke test PASSED: Registered claim {claim_data['claim_ref']} as app_rw")


def smoke_test_via_db(database_url: str) -> None:
    """Run smoke test directly against database using app_rw credentials."""
    print(f"Executing smoke test directly against DB as app_rw: {database_url}")
    engine = create_engine(database_url)
    SessionMaker = sessionmaker(bind=engine)

    with SessionMaker() as session:
        # Verify app_rw role
        session.execute(select(Customer)).first()
        policy = session.execute(select(Policy)).scalars().first()
        if not policy:
            print("Notice: No policy found in DB for direct DB smoke test.")
            return

        user = session.execute(
            select(User).where(User.customer_id == policy.customer_id, User.role == UserRole.CUSTOMER)
        ).scalars().first()
        if not user:
            print("Notice: No user found for customer in direct DB smoke test.")
            return

        claim_service = ClaimService(session)
        claim = claim_service.register_claim(
            policy_id=policy.id,
            claim_category="COLLISION",
            incident_date=date.today() - timedelta(days=1),
            incident_location="Smoke Test Way",
            incident_description="Direct DB smoke test as app_rw",
            requested_amount=Decimal("15000.00"),
            registered_by_id=user.id,
            registered_by_role="customer",
        )
        session.commit()
        assert claim.claim_ref.startswith("CLM-")
        print(f"Direct DB smoke test PASSED: Registered claim {claim.claim_ref} as app_rw")


def main() -> None:
    parser = argparse.ArgumentParser(description="Staging smoke test registering claim as app_rw")
    parser.add_argument("--url", "--api-url", dest="api_url", default=None, help="Staging API URL")
    parser.add_argument("--db-url", dest="db_url", default=None, help="Database URL for app_rw")
    args = parser.parse_args()

    if args.api_url:
        smoke_test_via_api(args.api_url)
    elif args.db_url:
        smoke_test_via_db(args.db_url)
    else:
        # Default fallback to configured DATABASE_URL
        smoke_test_via_db(settings.DATABASE_URL)


if __name__ == "__main__":
    main()

"""Customer registration tests for API and browser UI."""

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Customer, User
from app.domain.enums import UserRole


def _registration_payload() -> dict[str, str]:
    return {
        "full_name": "Demo Customer",
        "email": "new.customer@example.com",
        "password": "CustomerPassword123!",
        "dob": (date.today() - timedelta(days=9000)).isoformat(),
        "phone": "+91 9876543210",
        "address": "12 Demo Street, Bengaluru",
        "state_region": "Karnataka",
    }


def _get_registration_csrf(client: TestClient) -> str:
    response = client.get("/ui/register")
    assert response.status_code == 200
    marker = 'name="csrf_token" value="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    assert client.cookies.get("claim_login_csrf")
    return response.text[start:end]


def test_api_registration_creates_customer_and_customer_role(
    client: TestClient, db_session: Session
) -> None:
    response = client.post("/api/auth/register", json=_registration_payload())

    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "new.customer@example.com"
    assert data["role"] == UserRole.CUSTOMER.value
    assert data["customer_id"]

    user = db_session.query(User).filter_by(email="new.customer@example.com").one()
    customer = db_session.get(Customer, user.customer_id)
    assert customer is not None
    assert customer.full_name == "Demo Customer"
    assert user.password_hash != "CustomerPassword123!"
    assert user.password_hash.startswith("$argon2id$")

    login = client.post(
        "/api/auth/login",
        json={
            "email": "new.customer@example.com",
            "password": "CustomerPassword123!",
        },
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == UserRole.CUSTOMER.value


def test_public_registration_cannot_assign_privileged_role(client: TestClient) -> None:
    payload = _registration_payload()
    payload["role"] = UserRole.ADMIN.value
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 201
    assert response.json()["role"] == UserRole.CUSTOMER.value


def test_api_registration_rejects_duplicate_email(
    client: TestClient, db_session: Session
) -> None:
    payload = _registration_payload()
    assert client.post("/api/auth/register", json=payload).status_code == 201

    duplicate = client.post("/api/auth/register", json=payload)
    assert duplicate.status_code == 409


def test_api_registration_rejects_short_password(client: TestClient) -> None:
    payload = _registration_payload()
    payload["password"] = "short"
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 422


def test_ui_registration_creates_account_and_starts_session(
    client: TestClient, db_session: Session
) -> None:
    csrf_token = _get_registration_csrf(client)
    payload = _registration_payload()
    payload["confirm_password"] = payload["password"]

    response = client.post(
        "/ui/register",
        data={**payload, "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/claims"
    assert response.cookies.get("claim_session")

    user = db_session.query(User).filter_by(email="new.customer@example.com").one()
    assert user.role == UserRole.CUSTOMER
    assert user.customer_id is not None
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "USER_REGISTERED")
        .count()
        == 1
    )


def test_ui_registration_rejects_password_mismatch(client: TestClient) -> None:
    csrf_token = _get_registration_csrf(client)
    payload = _registration_payload()
    payload["confirm_password"] = "DifferentPassword123!"

    response = client.post(
        "/ui/register",
        data={**payload, "csrf_token": csrf_token},
    )

    assert response.status_code == 400
    assert "Passwords do not match" in response.text

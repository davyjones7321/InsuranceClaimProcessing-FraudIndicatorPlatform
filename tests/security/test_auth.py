"""Security and authentication tests."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import User
from app.security.csrf import generate_csrf_token, validate_csrf_token
from app.security.password import hash_password, verify_password
from app.security.rate_limit import AccountLockedError, check_rate_limit, record_login_attempt
from app.security.session import create_session, delete_session, get_user_from_session


def test_password_min_length_enforcement() -> None:
    """Password hashing must reject passwords shorter than 12 characters."""
    with pytest.raises(ValueError, match="at least 12 characters"):
        hash_password("short")

    valid_hash = hash_password("ValidPassword123!")
    assert valid_hash.startswith("$argon2id$")
    assert verify_password(valid_hash, "ValidPassword123!")
    assert not verify_password(valid_hash, "WrongPassword123!")


def test_login_rate_limiting_lockout(db_session: Session) -> None:
    """Login rate limiter locks account after 5 failed attempts."""
    identifier = "locked_user@test.com"

    # 4 failed attempts: not locked
    for _ in range(4):
        record_login_attempt(db_session, identifier, success=False)
    check_rate_limit(db_session, identifier)

    # 5th failed attempt: raises AccountLockedError
    record_login_attempt(db_session, identifier, success=False)
    with pytest.raises(AccountLockedError, match="Account temporarily locked"):
        check_rate_limit(db_session, identifier)


def test_database_backed_session_lifecycle(db_session: Session, sample_admin: User) -> None:
    """Session can be created, looked up, and deleted from the database."""
    session_id = create_session(db_session, sample_admin)
    assert session_id

    # Retrieve user from active session
    user = get_user_from_session(db_session, session_id)
    assert user is not None
    assert user.id == sample_admin.id

    # Delete session
    delete_session(db_session, session_id)
    assert get_user_from_session(db_session, session_id) is None


def test_csrf_token_validation() -> None:
    """CSRF tokens validate against the session ID and reject invalid/tampered tokens."""
    session_id = "test_session_token_123"
    token = generate_csrf_token(session_id)
    assert validate_csrf_token(session_id, token)

    # Reject for different session
    assert not validate_csrf_token("different_session", token)

    # Reject tampered token
    tampered = token[:-4] + "abcd"
    assert not validate_csrf_token(session_id, tampered)


def test_api_login_success_and_logout(client: TestClient, sample_admin: User) -> None:
    """Test full login API endpoint with cookie issuance, me profile check, and logout."""
    # 1. Login
    res = client.post(
        "/api/auth/login",
        json={"email": "admin@test.com", "password": "AdminSecurePassword123!"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "csrf_token" in data
    assert "session_id" in data
    assert data["user"]["email"] == "admin@test.com"
    assert "claim_session" in res.cookies

    # 2. Get me
    res_me = client.get("/api/auth/me", cookies=res.cookies)
    assert res_me.status_code == 200
    assert res_me.json()["email"] == "admin@test.com"

    # 3. Logout
    res_logout = client.post(
        "/api/auth/logout",
        cookies=res.cookies,
        headers={"X-CSRF-Token": data["csrf_token"]},
    )
    assert res_logout.status_code == 200

    # 4. Verify me fails after logout
    res_me_after = client.get("/api/auth/me", cookies=res.cookies)
    assert res_me_after.status_code == 401

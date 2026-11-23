"""Security and authentication tests."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import User
from app.security.csrf import generate_csrf_token, validate_csrf_token
from app.security.password import hash_password, verify_password
from app.security.rate_limit import AccountLockedError, check_rate_limit, record_login_attempt
from app.security.session import create_session, delete_session, get_user_from_session


def _get_ui_login_csrf(client: TestClient) -> str:
    """Fetch a valid anonymous login CSRF token and its bound cookie."""
    response = client.get("/ui/login")
    assert response.status_code == 200
    marker = 'name="csrf_token" value="'
    start = response.text.find(marker)
    assert start >= 0
    start += len(marker)
    end = response.text.find('"', start)
    assert end >= 0
    assert client.cookies.get("claim_login_csrf")
    return response.text[start:end]


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


def test_ui_login_uses_database_lockout(client: TestClient, db_session: Session) -> None:
    """UI login records failed attempts and enforces the shared API lockout threshold."""
    from app.db.models import LoginAttempt

    csrf_token = _get_ui_login_csrf(client)
    for _ in range(5):
        response = client.post(
            "/ui/login",
            data={
                "email": "missing-ui-user@test.com",
                "password": "WrongPassword123!",
                "csrf_token": csrf_token,
            },
        )
        assert response.status_code == 400

    attempts = (
        db_session.query(LoginAttempt)
        .filter(LoginAttempt.identifier == "missing-ui-user@test.com")
        .all()
    )
    assert len(attempts) == 5
    assert all(not attempt.success for attempt in attempts)

    locked_response = client.post(
        "/ui/login",
        data={
            "email": "missing-ui-user@test.com",
            "password": "WrongPassword123!",
            "csrf_token": csrf_token,
        },
    )
    assert locked_response.status_code == 429


def test_ui_login_rotates_session_and_writes_audit(
    client: TestClient, db_session: Session, sample_admin: User
) -> None:
    """Successful UI login invalidates the prior session and writes a WEB login event."""
    from app.audit.writer import _derive_chain_key
    from app.db.models import AuditLog

    csrf_token = _get_ui_login_csrf(client)
    old_session_id = create_session(db_session, sample_admin)
    db_session.commit()
    client.cookies.set("claim_session", old_session_id)

    response = client.post(
        "/ui/login",
        data={
            "email": " ADMIN@test.com ",
            "password": "AdminSecurePassword123!",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    new_session_id = response.cookies.get("claim_session")
    assert new_session_id
    assert new_session_id != old_session_id
    assert get_user_from_session(db_session, old_session_id) is None
    assert get_user_from_session(db_session, new_session_id) is not None

    login_event = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.chain_key == _derive_chain_key("User", str(sample_admin.id)),
            AuditLog.action == "USER_LOGIN",
        )
        .order_by(AuditLog.seq.desc())
        .first()
    )
    assert login_event is not None
    assert login_event.event_data["channel"] == "WEB"


def test_ui_login_rejects_missing_or_invalid_csrf(client: TestClient, db_session: Session) -> None:
    """Anonymous UI login requires a valid token bound to its pre-auth cookie."""
    from app.db.models import LoginAttempt

    missing = client.post(
        "/ui/login",
        data={"email": "missing-csrf@test.com", "password": "WrongPassword123!"},
    )
    assert missing.status_code == 422

    _get_ui_login_csrf(client)
    invalid = client.post(
        "/ui/login",
        data={
            "email": "missing-csrf@test.com",
            "password": "WrongPassword123!",
            "csrf_token": "invalid-token",
        },
    )
    assert invalid.status_code == 403
    assert (
        db_session.query(LoginAttempt)
        .filter(LoginAttempt.identifier == "missing-csrf@test.com")
        .count()
        == 0
    )


def test_ui_logout_requires_session_bound_csrf(
    client: TestClient, db_session: Session, sample_admin: User
) -> None:
    """UI logout refuses requests without the authenticated session's CSRF token."""
    session_id = create_session(db_session, sample_admin)
    db_session.commit()
    client.cookies.set("claim_session", session_id)

    rejected = client.post("/ui/logout", data={})
    assert rejected.status_code == 422
    assert get_user_from_session(db_session, session_id) is not None

    rejected_token = client.post("/ui/logout", data={"csrf_token": "invalid-token"})
    assert rejected_token.status_code == 403
    assert get_user_from_session(db_session, session_id) is not None

    accepted = client.post(
        "/ui/logout",
        data={"csrf_token": generate_csrf_token(session_id)},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert get_user_from_session(db_session, session_id) is None

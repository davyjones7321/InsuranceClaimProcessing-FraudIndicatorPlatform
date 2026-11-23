"""Tests for health, readiness, operational metrics, request ID, and structured logging."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.observability import mask_pii
from app.rules.loader import ACTIVE_RULESET_SHA256


def test_healthz_liveness_probe(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["app"] == settings.APP_NAME
    assert data["version"] == settings.APP_VERSION


def test_readyz_readiness_probe(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"
    assert data["ruleset_version"] == settings.ACTIVE_RULESET_VERSION
    assert data["ruleset_sha256"] == ACTIVE_RULESET_SHA256
    assert data["engine_version"] == settings.ENGINE_VERSION


def test_metrics_endpoint(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["app"] == settings.APP_NAME
    assert "total_claims" in data
    assert "claims_by_status" in data
    assert "audit_chains_count" in data
    assert data["validation_rules_count"] >= 12
    assert data["risk_indicators_count"] >= 9


def test_request_id_middleware_generates_and_propagates(client: TestClient) -> None:
    # 1. Automatic generation when header not supplied
    resp = client.get("/healthz")
    assert "x-request-id" in resp.headers
    assert len(resp.headers["x-request-id"]) > 10

    # 2. Propagation when header is explicitly supplied
    custom_id = "custom-test-request-id-12345"
    resp_custom = client.get("/healthz", headers={"X-Request-ID": custom_id})
    assert resp_custom.headers.get("x-request-id") == custom_id


def test_security_headers_present(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "Content-Security-Policy" in resp.headers


def test_pii_masking_utility() -> None:
    # Mask emails
    raw_email = "User customer@insurance.example submitted claim"
    masked_email = mask_pii(raw_email)
    assert "customer@insurance.example" not in masked_email
    assert "c***@insurance.example" in masked_email

    # Mask phones
    raw_phone = "Contact customer at 123-456-7890 for verification"
    masked_phone = mask_pii(raw_phone)
    assert "123-456-7890" not in masked_phone
    assert "7890" in masked_phone

    # Mask secrets
    raw_secret = 'Payload contains password: "SecretPassword123!"'
    masked_secret = mask_pii(raw_secret)
    assert "SecretPassword123!" not in masked_secret
    assert "******" in masked_secret


@pytest.mark.parametrize(
    ("revision", "expected_status"),
    [
        ("0010_schema_alignment", 200),
        ("0008_operations_jobs", 503),
    ],
)
def test_readyz_requires_current_alembic_head(
    client: TestClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    revision: str,
    expected_status: int,
) -> None:
    """Staging readiness accepts the current schema head and rejects older revisions."""
    monkeypatch.setattr(settings, "ENV", "staging")
    db_session.execute(
        text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
    )
    db_session.execute(text("DELETE FROM alembic_version"))
    db_session.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
        {"revision": revision},
    )
    db_session.commit()

    response = client.get("/readyz")
    assert response.status_code == expected_status
    if expected_status == 503:
        assert "0010_schema_alignment" in response.json()["detail"]

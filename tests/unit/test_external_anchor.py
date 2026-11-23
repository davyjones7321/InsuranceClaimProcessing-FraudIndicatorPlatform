"""Unit tests for external S3 Object Lock audit anchoring (Decision D3).

Enforces:
1. Manifests are never written into the git repository by default.
2. S3 Object Lock in COMPLIANCE mode is used for daily anchors.
3. Separation of duties: writer credentials (PutObject only) vs verifier credentials (read-only).
4. Verifier retrieves unbroken manifest history from external storage.
"""

from datetime import UTC, datetime, timedelta
import json
import os
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.audit.anchor import (
    DEFAULT_ANCHOR_DIR,
    S3AnchorClient,
    create_daily_anchor_manifest,
    get_latest_anchor_manifest,
)


def test_default_anchor_dir_is_none() -> None:
    """Verify that default anchor storage does not write into the git repository."""
    assert DEFAULT_ANCHOR_DIR is None


def test_create_daily_anchor_manifest_does_not_write_to_repo(db_session: Session) -> None:
    """When anchor_dir is None and S3 unconfigured, no manifest files are written to disk."""
    manifest = create_daily_anchor_manifest(db_session, anchor_dir=None, manifest_date="2026-11-22")
    assert manifest is not None
    assert manifest["manifest_date"] == "2026-11-22"
    assert "root_hash" in manifest

    # Assert repo path docs/evidence/anchors does not exist or has no newly written files
    repo_anchor_dir = os.path.join("docs", "evidence", "anchors")
    assert not os.path.exists(repo_anchor_dir)


def test_create_daily_anchor_manifest_writes_to_explicit_dir(db_session: Session) -> None:
    """When an explicit anchor_dir is provided (e.g. for testing), manifest is saved there."""
    with tempfile.TemporaryDirectory() as temp_dir:
        manifest = create_daily_anchor_manifest(
            db_session, anchor_dir=temp_dir, manifest_date="2026-11-22"
        )
        assert manifest is not None
        files = os.listdir(temp_dir)
        assert len(files) == 1
        assert files[0].startswith("manifest_2026-11-22_")


def test_s3_anchor_client_configuration() -> None:
    """Verify credential separation properties on S3AnchorClient."""
    client_unconfigured = S3AnchorClient(
        bucket="",
        writer_access_key="",
        writer_secret_key="",
        verifier_access_key="",
        verifier_secret_key="",
    )
    assert not client_unconfigured.is_configured
    assert not client_unconfigured.is_verifier_configured

    client = S3AnchorClient(
        bucket="audit-lock-bucket",
        writer_access_key="AKIA_WRITER",
        writer_secret_key="SECRET_WRITER",
        verifier_access_key="AKIA_VERIFIER",
        verifier_secret_key="SECRET_VERIFIER",
    )
    assert client.is_configured
    assert client.is_verifier_configured


def test_s3_anchor_client_put_manifest_object_lock_headers() -> None:
    """Verify PutObject request sends compliance mode Object Lock headers with writer credentials."""
    client = S3AnchorClient(
        bucket="audit-compliance-bucket",
        region="ap-south-1",
        endpoint_url="https://audit-compliance-bucket.s3.ap-south-1.amazonaws.com",
        writer_access_key="AKIA_D3_WRITER",
        writer_secret_key="SEC_D3_WRITER_KEY",
        verifier_access_key="AKIA_D3_VERIFIER",
        verifier_secret_key="SEC_D3_VERIFIER_KEY",
        retention_days=3650,
    )

    manifest_data = {
        "manifest_date": "2026-11-22",
        "root_hash": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "chain_heads": {},
        "access_log": {"count": 0, "digest": "0" * 64},
        "created_at": datetime.now(UTC).isoformat(),
        "previous_manifest_hash": "genesis",
        "version": 1,
        "watermark": datetime.now(UTC).isoformat(),
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.Client.put", return_value=mock_resp) as mock_put:
        result_uri = client.put_manifest(manifest_data)

        assert mock_put.called
        call_args = mock_put.call_args
        target_url = call_args[0][0]
        headers = call_args[1]["headers"]

        # 1. Target URL matches S3 object key
        assert target_url == "https://audit-compliance-bucket.s3.ap-south-1.amazonaws.com/manifests/manifest_2026-11-22_a1b2c3d4e5f6.json"
        assert result_uri == "s3://audit-compliance-bucket/manifests/manifest_2026-11-22_a1b2c3d4e5f6.json"

        # 2. Object Lock Compliance mode headers
        assert headers["x-amz-object-lock-mode"] == "COMPLIANCE"
        assert "x-amz-object-lock-retain-until-date" in headers
        assert "content-md5" in headers

        # 3. Signed with WRITER credentials (Decision D3)
        assert "AKIA_D3_WRITER" in headers["authorization"]
        assert "AKIA_D3_VERIFIER" not in headers["authorization"]


def test_s3_anchor_client_get_manifest_verifier_credentials() -> None:
    """Verify GetObject request uses read-only verifier credentials."""
    client = S3AnchorClient(
        bucket="audit-compliance-bucket",
        region="ap-south-1",
        endpoint_url="https://audit-compliance-bucket.s3.ap-south-1.amazonaws.com",
        writer_access_key="AKIA_D3_WRITER",
        writer_secret_key="SEC_D3_WRITER_KEY",
        verifier_access_key="AKIA_D3_VERIFIER",
        verifier_secret_key="SEC_D3_VERIFIER_KEY",
    )

    manifest_payload = {
        "manifest_date": "2026-11-22",
        "root_hash": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = manifest_payload

    with patch("httpx.Client.get", return_value=mock_resp) as mock_get:
        data = client.get_manifest("manifests/manifest_2026-11-22_a1b2c3d4e5f6.json")
        assert data["root_hash"] == "a1b2c3d4e5f60718293a4b5c6d7e8f90"

        call_args = mock_get.call_args
        headers = call_args[1]["headers"]

        # Signed with VERIFIER credentials (Decision D3)
        assert "AKIA_D3_VERIFIER" in headers["authorization"]
        assert "AKIA_D3_WRITER" not in headers["authorization"]


def test_s3_anchor_client_get_latest_manifest_parses_xml() -> None:
    """Verify ListObjectsV2 parsing finds and retrieves the latest manifest."""
    client = S3AnchorClient(
        bucket="audit-compliance-bucket",
        region="ap-south-1",
        endpoint_url="https://audit-compliance-bucket.s3.ap-south-1.amazonaws.com",
        writer_access_key="AKIA_D3_WRITER",
        writer_secret_key="SEC_D3_WRITER_KEY",
        verifier_access_key="AKIA_D3_VERIFIER",
        verifier_secret_key="SEC_D3_VERIFIER_KEY",
    )

    xml_response = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
        <Contents>
            <Key>manifests/manifest_2026-11-20_111111111111.json</Key>
        </Contents>
        <Contents>
            <Key>manifests/manifest_2026-11-21_222222222222.json</Key>
        </Contents>
    </ListBucketResult>
    """

    mock_list_resp = MagicMock()
    mock_list_resp.status_code = 200
    mock_list_resp.text = xml_response

    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {
        "manifest_date": "2026-11-21",
        "root_hash": "22222222222233334444555566667777",
    }

    def mock_get(url: str, headers: dict[str, str]) -> MagicMock:
        if "list-type=2" in url:
            return mock_list_resp
        return mock_get_resp

    with patch("httpx.Client.get", side_effect=mock_get):
        latest = client.get_latest_manifest()
        assert latest is not None
        assert latest["manifest_date"] == "2026-11-21"

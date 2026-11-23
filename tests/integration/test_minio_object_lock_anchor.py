"""Integration test for S3AnchorClient against MinIO with Object Lock (Decision D3).

Tests:
1. Write manifest to MinIO bucket with Object Lock in COMPLIANCE mode using writer credentials.
2. Attempt overwrite of existing locked manifest (must fail).
3. Attempt DELETE of locked manifest (must fail with ObjectLockedError / AccessDenied).
4. Verifier credentials are read-only (can GET/List, cannot PUT/DELETE).
"""

import os
from datetime import UTC, datetime

import httpx
import pytest

from app.audit.anchor import S3AnchorClient, _sign_sigv4


def is_minio_available(endpoint_url: str) -> bool:
    """Check if MinIO server is reachable."""
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{endpoint_url.rstrip('/')}/minio/health/live")
            return resp.status_code == 200
    except Exception:
        return False


def test_s3_anchor_client_minio_object_lock() -> None:
    """Test S3AnchorClient against MinIO with Object Lock enabled.

    If MINIO_ENDPOINT_URL is not set or MinIO is unreachable, test reports UNVERIFIED
    per requirement 5.
    """
    endpoint_url = os.environ.get("MINIO_ENDPOINT_URL") or "http://localhost:9000"
    if not is_minio_available(endpoint_url):
        pytest.skip("UNVERIFIED: MinIO instance with Object Lock is not reachable in this environment")

    bucket = os.environ.get("MINIO_BUCKET", "audit-anchors-compliance")
    writer_ak = os.environ.get("MINIO_WRITER_AK", "minio_writer")
    writer_sk = os.environ.get("MINIO_WRITER_SK", "minio_writer_secret")
    verifier_ak = os.environ.get("MINIO_VERIFIER_AK", "minio_verifier")
    verifier_sk = os.environ.get("MINIO_VERIFIER_SK", "minio_verifier_secret")

    client = S3AnchorClient(
        bucket=bucket,
        endpoint_url=endpoint_url,
        region="us-east-1",
        writer_access_key=writer_ak,
        writer_secret_key=writer_sk,
        verifier_access_key=verifier_ak,
        verifier_secret_key=verifier_sk,
        retention_days=30,
    )

    manifest_data = {
        "manifest_date": "2026-11-23",
        "root_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "chain_heads": {"claim:test-1": 1},
        "created_at": datetime.now(UTC).isoformat(),
        "version": 1,
    }

    # 1. Write manifest using writer credentials
    s3_uri = client.put_manifest(manifest_data)
    assert s3_uri.startswith(f"s3://{bucket}/manifests/")
    key = s3_uri.replace(f"s3://{bucket}/", "")

    # 2. Attempt overwrite (must fail due to Object Lock)
    tampered_manifest = dict(manifest_data, root_hash="0000000000000000000000000000000000000000000000000000000000000000")
    with pytest.raises(Exception, match=r"(ObjectLocked|AccessDenied|403|precondition|prohibited)"):
        client.put_manifest(tampered_manifest)

    # 3. Attempt DELETE (must fail due to Object Lock COMPLIANCE)
    del_url, del_headers = _sign_sigv4(
        method="DELETE",
        endpoint_url=endpoint_url,
        path=f"/{bucket}/{key}",
        headers={},
        payload=b"",
        access_key=writer_ak,
        secret_key=writer_sk,
        region="us-east-1",
        service="s3",
    )
    with httpx.Client(timeout=5.0) as http_client:
        del_resp = http_client.delete(del_url, headers=del_headers)
        assert del_resp.status_code in (403, 400), f"Delete should be rejected, got {del_resp.status_code}"

    # 4. Verifier read-only (GET succeeds)
    retrieved = client.get_manifest(f"{bucket}/{key}")
    assert retrieved["root_hash"] == manifest_data["root_hash"]

    # 5. Verifier attempt write (must fail - read-only)
    v_url, v_headers = _sign_sigv4(
        method="PUT",
        endpoint_url=endpoint_url,
        path=f"/{bucket}/manifests/test_unauthorized.json",
        headers={"content-type": "application/json"},
        payload=b"{}",
        access_key=verifier_ak,
        secret_key=verifier_sk,
        region="us-east-1",
        service="s3",
    )
    with httpx.Client(timeout=5.0) as http_client:
        v_resp = http_client.put(v_url, headers=v_headers, content=b"{}")
        assert v_resp.status_code in (403, 401)

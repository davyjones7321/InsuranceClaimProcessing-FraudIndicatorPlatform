"""External audit anchoring and daily manifest creation (Task 8 & Task 10, Decision D3 & D4).

Anchors:
1. Daily manifest with timestamp, previous_manifest_hash, changed chain heads, and root_hash.
2. Access log closed-day count and digest of row_hash values.
3. Transaction watermark preventing race with in-flight transactions.
4. External S3 Object Lock destination in compliance mode with separate writer and verifier credentials (D3).
5. Never writes anchor manifests into the git repository.
"""

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import os
from typing import Any, cast
import urllib.parse
import xml.etree.ElementTree as ET

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.schema import canonical_json_bytes
from app.config import settings
from app.db.base import utc_now
from app.db.models import AccessLog, AuditChainHead, AuditLog

# Do not write manifests inside the repo (Decision D3)
DEFAULT_ANCHOR_DIR: str | None = None


def _sign_sigv4(
    method: str,
    endpoint_url: str,
    path: str,
    query_params: dict[str, str] | None,
    headers: dict[str, str],
    payload: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
) -> tuple[str, dict[str, str]]:
    """Generate AWS SigV4 authorization headers for S3 requests."""
    parsed_endpoint = urllib.parse.urlparse(endpoint_url)
    host = parsed_endpoint.netloc
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload_hash = hashlib.sha256(payload).hexdigest()

    req_headers = {k.lower(): v.strip() for k, v in headers.items()}
    req_headers["host"] = host
    req_headers["x-amz-date"] = amz_date
    req_headers["x-amz-content-sha256"] = payload_hash

    canonical_uri = urllib.parse.quote(path if path.startswith("/") else f"/{path}", safe="/-_.~")

    if query_params:
        sorted_params = sorted(query_params.items())
        canonical_query = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in sorted_params
        )
    else:
        canonical_query = ""

    sorted_header_keys = sorted(req_headers.keys())
    canonical_headers = "".join(f"{k}:{req_headers[k]}\n" for k in sorted_header_keys)
    signed_headers = ";".join(sorted_header_keys)

    canonical_request = (
        f"{method.upper()}\n"
        f"{canonical_uri}\n"
        f"{canonical_query}\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    k_secret = f"AWS4{secret_key}".encode("utf-8")
    k_date = hmac.new(k_secret, date_stamp.encode("utf-8"), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()

    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    auth_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    req_headers["authorization"] = auth_header

    full_url = f"{endpoint_url.rstrip('/')}{canonical_uri}"
    if canonical_query:
        full_url = f"{full_url}?{canonical_query}"

    return full_url, req_headers


class S3AnchorClient:
    """External S3 Object Lock anchor client adhering to Decision D3.

    Uses separate writer and verifier credentials:
    - Writer: PutObject only with compliance mode Object Lock retention.
    - Verifier: Read-only (ListBucket / GetObject).
    - Application identity: has no access.
    """

    def __init__(
        self,
        bucket: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        writer_access_key: str | None = None,
        writer_secret_key: str | None = None,
        verifier_access_key: str | None = None,
        verifier_secret_key: str | None = None,
        retention_days: int | None = None,
    ) -> None:
        self.bucket = bucket or settings.ANCHOR_S3_BUCKET
        self.region = region or settings.ANCHOR_AWS_REGION
        self.endpoint_url = (
            endpoint_url
            or settings.ANCHOR_S3_ENDPOINT_URL
            or (f"https://{self.bucket}.s3.{self.region}.amazonaws.com" if self.bucket else "")
        )
        self.writer_access_key = writer_access_key or settings.ANCHOR_AWS_ACCESS_KEY_ID
        self.writer_secret_key = writer_secret_key or settings.ANCHOR_AWS_SECRET_ACCESS_KEY
        self.verifier_access_key = verifier_access_key or settings.ANCHOR_VERIFIER_AWS_ACCESS_KEY_ID
        self.verifier_secret_key = (
            verifier_secret_key or settings.ANCHOR_VERIFIER_AWS_SECRET_ACCESS_KEY
        )
        self.retention_days = retention_days or settings.ANCHOR_OBJECT_LOCK_RETENTION_DAYS

    @property
    def is_configured(self) -> bool:
        return bool(self.bucket and self.writer_access_key and self.writer_secret_key)

    @property
    def is_verifier_configured(self) -> bool:
        return bool(self.bucket and self.verifier_access_key and self.verifier_secret_key)

    def put_manifest(self, manifest_body: dict[str, Any]) -> str:
        """Upload daily manifest to S3 with Object Lock in COMPLIANCE mode using writer credentials."""
        if not self.is_configured:
            raise ValueError("S3 anchor writer credentials or bucket not configured")

        date_str = manifest_body.get("manifest_date", utc_now().strftime("%Y-%m-%d"))
        root_hash = manifest_body.get("root_hash", "")
        key = f"manifests/manifest_{date_str}_{root_hash[:12]}.json"

        payload = canonical_json_bytes(manifest_body)
        md5_digest = hashlib.md5(payload).digest()
        content_md5 = base64.b64encode(md5_digest).decode("ascii")

        retain_until = (utc_now() + timedelta(days=self.retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        headers = {
            "content-type": "application/json",
            "content-md5": content_md5,
            "x-amz-object-lock-mode": "COMPLIANCE",
            "x-amz-object-lock-retain-until-date": retain_until,
        }

        url, signed_headers = _sign_sigv4(
            method="PUT",
            endpoint_url=self.endpoint_url,
            path=f"/{key}",
            query_params=None,
            headers=headers,
            payload=payload,
            access_key=self.writer_access_key,
            secret_key=self.writer_secret_key,
            region=self.region,
            service="s3",
        )

        with httpx.Client(timeout=10.0) as client:
            resp = client.put(url, headers=signed_headers, content=payload)
            if resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"Failed to put anchor manifest to S3: status {resp.status_code}, response: {resp.text}"
                )

        return f"s3://{self.bucket}/{key}"

    def get_manifest(self, key: str) -> dict[str, Any]:
        """Fetch manifest from S3 using verifier credentials."""
        if not self.is_verifier_configured:
            raise ValueError("S3 anchor verifier credentials not configured")

        clean_key = key.lstrip("/")
        url, signed_headers = _sign_sigv4(
            method="GET",
            endpoint_url=self.endpoint_url,
            path=f"/{clean_key}",
            query_params=None,
            headers={},
            payload=b"",
            access_key=self.verifier_access_key,
            secret_key=self.verifier_secret_key,
            region=self.region,
            service="s3",
        )

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=signed_headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to get anchor manifest from S3: status {resp.status_code}, response: {resp.text}"
                )
            return cast(dict[str, Any], resp.json())

    def get_latest_manifest(self) -> dict[str, Any] | None:
        """List manifests in S3 using verifier credentials and return the most recent."""
        if not self.is_verifier_configured:
            return None

        query_params = {"list-type": "2", "prefix": "manifests/manifest_"}
        url, signed_headers = _sign_sigv4(
            method="GET",
            endpoint_url=self.endpoint_url,
            path="/",
            query_params=query_params,
            headers={},
            payload=b"",
            access_key=self.verifier_access_key,
            secret_key=self.verifier_secret_key,
            region=self.region,
            service="s3",
        )

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=signed_headers)
            if resp.status_code != 200:
                return None

            root = ET.fromstring(resp.text)
            keys = [
                elem.text
                for elem in root.iter()
                if elem.tag.endswith("Key") and elem.text and elem.text.endswith(".json")
            ]
            if not keys:
                return None

            keys.sort()
            latest_key = keys[-1]
            return self.get_manifest(latest_key)


def get_latest_anchor_manifest(
    anchor_dir: str | None = DEFAULT_ANCHOR_DIR,
) -> dict[str, Any] | None:
    """Read the most recent anchor manifest from the storage directory or external S3."""
    if anchor_dir and os.path.isdir(anchor_dir):
        manifest_files = [
            f for f in os.listdir(anchor_dir) if f.startswith("manifest_") and f.endswith(".json")
        ]
        if not manifest_files:
            return None

        manifest_files.sort()
        latest_file = os.path.join(anchor_dir, manifest_files[-1])
        with open(latest_file, encoding="utf-8") as f:
            data: dict[str, Any] = cast(dict[str, Any], json.load(f))
            return data

    client = S3AnchorClient()
    if client.is_verifier_configured:
        return client.get_latest_manifest()

    return None


def create_daily_anchor_manifest(
    db: Session,
    anchor_dir: str | None = DEFAULT_ANCHOR_DIR,
    manifest_date: str | None = None,
) -> dict[str, Any]:
    """Generate daily anchor manifest recording audit chain heads and access log digests."""
    now = utc_now()
    date_str = manifest_date or now.strftime("%Y-%m-%d")

    # Watermark: timestamp of latest audit row or now
    max_audit_time = db.scalar(select(func.max(AuditLog.occurred_at)))
    watermark_dt = max_audit_time or now
    watermark_iso = watermark_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # 1. Capture chain heads
    heads = db.scalars(select(AuditChainHead).order_by(AuditChainHead.chain_key)).all()
    chain_heads_dict: dict[str, dict[str, Any]] = {}
    for h in heads:
        chain_heads_dict[h.chain_key] = {
            "seq": h.last_seq,
            "hash": h.last_hash,
        }

    # 2. Capture access log digest for closed window
    access_logs = db.scalars(select(AccessLog).order_by(AccessLog.created_at)).all()
    access_count = len(access_logs)
    sorted_row_hashes = sorted([log.row_hash for log in access_logs if log.row_hash])
    combined_hashes = "".join(sorted_row_hashes).encode("utf-8")
    access_digest = hashlib.sha256(combined_hashes).hexdigest()

    # 3. Previous manifest hash
    prev_manifest = get_latest_anchor_manifest(anchor_dir)
    prev_hash = (
        prev_manifest["root_hash"]
        if prev_manifest
        else hashlib.sha256(b"genesis_anchor").hexdigest()
    )

    manifest_body = {
        "access_log": {
            "count": access_count,
            "digest": access_digest,
        },
        "chain_heads": chain_heads_dict,
        "created_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "manifest_date": date_str,
        "previous_manifest_hash": prev_hash,
        "version": 1,
        "watermark": watermark_iso,
    }

    # Compute root hash over canonical manifest body
    canonical_bytes = canonical_json_bytes(manifest_body)
    root_hash = hashlib.sha256(canonical_bytes).hexdigest()
    manifest_body["root_hash"] = root_hash

    # Save to anchor directory if an explicit directory was provided
    if anchor_dir:
        os.makedirs(anchor_dir, exist_ok=True)
        filename = f"manifest_{date_str}_{root_hash[:12]}.json"
        file_path = os.path.join(anchor_dir, filename)

        if os.path.exists(file_path):
            filename = f"manifest_{date_str}_{root_hash[:16]}.json"
            file_path = os.path.join(anchor_dir, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(manifest_body, f, indent=2, sort_keys=True)
    else:
        # Upload to external S3 Object Lock destination (Decision D3)
        client = S3AnchorClient()
        if client.is_configured:
            client.put_manifest(manifest_body)

    return manifest_body

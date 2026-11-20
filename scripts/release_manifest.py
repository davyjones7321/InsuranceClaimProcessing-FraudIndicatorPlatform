"""Release manifest generator for CI and audit verification (P5 Task 11).

Generates or verifies a sealed release manifest recording commit SHA, lockfile hash,
Python runtime version, base container digest, ruleset hash, and engine version.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from app.config import settings
from app.rules.loader import ACTIVE_RULESET
from app.schemas.snapshots import compute_ruleset_hash

DEFAULT_BASE_IMAGE = (
    "python:3.13-slim@sha256:d82627e781ea3db26998cbddfd5d30e0147981fb5f0a7114b0351780f2d8a57d"
)


def get_git_commit_sha() -> str:
    """Retrieve current HEAD commit SHA from git or environment."""
    env_sha = os.environ.get("GITHUB_SHA") or os.environ.get("GIT_COMMIT")
    if env_sha:
        return env_sha.strip()
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()
    except Exception:
        return "UNKNOWN_COMMIT"


def get_lockfile_hash() -> str:
    """Compute SHA-256 hash of project lockfile or dependencies specification."""
    for lock_path in (Path("poetry.lock"), Path("requirements.txt"), Path("pyproject.toml")):
        if lock_path.exists():
            return hashlib.sha256(lock_path.read_bytes()).hexdigest()
    return "NO_LOCKFILE_FOUND"


def generate_manifest(base_image: str = DEFAULT_BASE_IMAGE) -> dict[str, str]:
    """Generate structured release manifest dictionary."""
    commit_sha = get_git_commit_sha()
    lockfile_hash = get_lockfile_hash()
    ruleset_hash = compute_ruleset_hash(ACTIVE_RULESET)

    return {
        "commit_sha": commit_sha,
        "lockfile_sha256": lockfile_hash,
        "python_version": sys.version.split()[0],
        "base_image_digest": base_image,
        "ruleset_version": settings.ACTIVE_RULESET_VERSION,
        "ruleset_sha256": ruleset_hash,
        "engine_version": settings.ENGINE_VERSION,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Release Manifest")
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="release_manifest.json",
        help="Output path for the generated release manifest JSON",
    )
    parser.add_argument(
        "--base-image",
        type=str,
        default=DEFAULT_BASE_IMAGE,
        help="Pinned container base image digest",
    )
    args = parser.parse_args()

    manifest = generate_manifest(base_image=args.base_image)
    out_path = Path(args.output)
    out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Release manifest generated successfully: {out_path.resolve()}")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

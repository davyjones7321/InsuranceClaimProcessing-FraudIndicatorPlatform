"""Versioned YAML ruleset loader with SHA-256 verification and float rejection."""

import hashlib
from pathlib import Path
from typing import Any

import yaml

from app.schemas.ruleset import validate_ruleset_dict


class RulesetError(Exception):
    """Raised when ruleset loading or validation fails."""

    pass


def _reject_floats(data: Any, path: str = "") -> None:
    """Ensure no float values exist in the parsed ruleset structure."""
    if isinstance(data, float):
        raise RulesetError(f"Float value encountered at ruleset path '{path}': {data}")
    if isinstance(data, dict):
        for k, v in data.items():
            _reject_floats(v, f"{path}.{k}" if path else str(k))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            _reject_floats(item, f"{path}[{idx}]")


def load_ruleset_from_string(content: str) -> tuple[dict[str, Any], str]:
    """Parse ruleset YAML content and return (ruleset_dict, content_sha256)."""
    sha256_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        parsed = yaml.safe_load(content)
    except Exception as exc:
        raise RulesetError(f"Failed to parse ruleset YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise RulesetError("Ruleset content must be a YAML mapping")

    _reject_floats(parsed)

    required_keys = {
        "ruleset_version",
        "engine_version",
        "validation_rules",
        "risk_groups",
        "risk_indicators",
        "risk_thresholds",
    }
    missing = required_keys - set(parsed.keys())
    if missing:
        raise RulesetError(f"Ruleset is missing mandatory top-level keys: {missing}")

    try:
        validate_ruleset_dict(parsed)
    except Exception as exc:
        raise RulesetError(f"Ruleset schema validation failed: {exc}") from exc

    return parsed, sha256_hash


def load_ruleset(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read ruleset file from disk, parse, and verify."""
    file_path = Path(path)
    if not file_path.is_file():
        raise RulesetError(f"Ruleset file '{file_path}' does not exist")
    content = file_path.read_text(encoding="utf-8")
    return load_ruleset_from_string(content)


# Default active ruleset v1 loaded from package directory
DEFAULT_RULESET_PATH = Path(__file__).parent / "ruleset_v1.yaml"
ACTIVE_RULESET, ACTIVE_RULESET_SHA256 = load_ruleset(DEFAULT_RULESET_PATH)

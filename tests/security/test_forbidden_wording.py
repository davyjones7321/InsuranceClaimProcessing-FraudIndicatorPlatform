"""Security and compliance test verifying zero accusatory or fraud-declaring wording."""

import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent.parent / "app"

# Prohibited accusatory terms per Section 10 and Acceptance Checklist item 5
BANNED_PATTERNS = [
    re.compile(r"\bfraud\s+detected\b", re.IGNORECASE),
    re.compile(r"\bfraudulent\b", re.IGNORECASE),
    re.compile(r"\bfraudster\b", re.IGNORECASE),
    re.compile(r"\bscam\b", re.IGNORECASE),
    re.compile(r"\bscammer\b", re.IGNORECASE),
    re.compile(r"\bguilty\b", re.IGNORECASE),
    re.compile(r"\bcriminal\b", re.IGNORECASE),
]


def test_engine_and_api_contain_zero_forbidden_accusatory_wording() -> None:
    """Scan all engine, API, schema, and service files for banned accusatory vocabulary."""
    violations: list[str] = []

    target_subdirs = ["engine", "api", "schemas", "rules", "services"]

    for subdir in target_subdirs:
        dir_path = APP_DIR / subdir
        if not dir_path.is_dir():
            continue

        for file_path in dir_path.glob("**/*"):
            if file_path.suffix not in (".py", ".yaml", ".yml", ".html", ".jinja2"):
                continue

            content = file_path.read_text(encoding="utf-8")
            for line_no, line in enumerate(content.splitlines(), start=1):
                # Ignore comments referencing the ban rule itself
                if "forbidden" in line.lower() or "banned" in line.lower():
                    continue

                for pattern in BANNED_PATTERNS:
                    if pattern.search(line):
                        violations.append(
                            f"{file_path.relative_to(APP_DIR.parent)}:{line_no}: "
                            f"Contains banned term matching '{pattern.pattern}': {line.strip()}"
                        )

    assert not violations, "Accusatory wording violations detected:\n" + "\n".join(violations)

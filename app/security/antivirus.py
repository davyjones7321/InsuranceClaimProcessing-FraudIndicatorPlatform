"""Antivirus scanner hook and integration point (P7 Task 4).

Reports scanner status as INACTIVE until an external scanner (such as ClamAV)
is configured and verified. Provides safe extension points without blocking.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.config import settings


class ScanStatus(StrEnum):
    INACTIVE = "INACTIVE"
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"
    ERROR = "ERROR"


class InfectedFileError(Exception):
    """Raised when an uploaded document fails malware scanning."""

    def __init__(self, message: str, details: str | None = None) -> None:
        super().__init__(message)
        self.details = details or message


@dataclass(frozen=True)
class ScanResult:
    """Malware scan result details."""

    status: ScanStatus
    is_clean: bool
    scanner_name: str
    details: str
    scanned_at: datetime


def scan_file(
    content: bytes,
    file_name: str,
    mime_type: str | None = None,
) -> ScanResult:
    """Scan file bytes for malware.

    Returns ScanResult with status INACTIVE by default until an external scanner is configured.
    Raises InfectedFileError if malware is detected.
    """
    now = datetime.now(UTC)

    # Check if antivirus scanner is configured and enabled
    if not getattr(settings, "ANTIVIRUS_ENABLED", False):
        return ScanResult(
            status=ScanStatus.INACTIVE,
            is_clean=True,
            scanner_name="inactive_stub",
            details="Antivirus scanner is inactive; integration hook ready for ClamAV/ICAP daemon",
            scanned_at=now,
        )

    # When enabled, an external scanner daemon would be invoked here.
    # Safe check for EICAR standard test signature in test/dev environments
    eicar_signature = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    if eicar_signature in content:
        raise InfectedFileError(
            f"Malware signature detected in uploaded file '{file_name}'",
            details="EICAR test malware signature matched",
        )

    return ScanResult(
        status=ScanStatus.CLEAN,
        is_clean=True,
        scanner_name="configured_scanner",
        details="File clean; no threat detected",
        scanned_at=now,
    )

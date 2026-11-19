"""Observability, structured JSON logging, and PII masking."""

import json
import logging
import re
import sys
from typing import Any

EMAIL_PATTERN = re.compile(r"\b([a-zA-Z0-9_.+-])[a-zA-Z0-9_.+-]*@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)\b")
PHONE_PATTERN = re.compile(r"\b(\+?\d{1,3}[-.\s]?)?(\(?\d{3}\)?[-.\s]?)(\d{3})[-.\s]?(\d{4})\b")
SECRET_KEY_PATTERN = re.compile(
    r"(password|secret|token|api_key|authorization)[\"']?\s*[:=]\s*[\"']?([^\"'\s,]+)",
    re.IGNORECASE,
)


def mask_pii(text: str) -> str:
    """Mask email addresses, phone numbers, and secrets in strings."""
    if not text:
        return text

    # Mask emails: j***@example.com
    masked = EMAIL_PATTERN.sub(r"\1***@\2", text)
    # Mask phones: +1-***-***-1234
    masked = PHONE_PATTERN.sub(r"\1***-***-\4", masked)
    # Mask secrets
    masked = SECRET_KEY_PATTERN.sub(r'\1: "******"', masked)
    return masked


class JSONLogFormatter(logging.Formatter):
    """Format log records as single-line JSON objects with PII masking."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_pii(record.getMessage()),
        }

        # Include request_id if present
        request_id = getattr(record, "request_id", None)
        if request_id:
            log_data["request_id"] = request_id

        # Include extra structured fields if present
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                if isinstance(v, str):
                    log_data[k] = mask_pii(v)
                else:
                    log_data[k] = v

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root logger to output structured JSON."""
    logger = logging.getLogger("claim_platform")
    logger.setLevel(level)

    # Avoid duplicate handlers
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONLogFormatter())
        logger.addHandler(handler)

    logger.propagate = False
    return logger


logger = setup_logging()

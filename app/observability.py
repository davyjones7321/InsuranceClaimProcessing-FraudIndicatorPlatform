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


class MetricsCollector:
    """In-memory operational metrics accumulator."""

    def __init__(self) -> None:
        import time

        self.request_count: int = 0
        self.error_5xx_count: int = 0
        self.total_latency_ms: float = 0.0
        self.start_time: float = time.time()

    def record_request(self, duration_ms: float, status_code: int) -> None:
        self.request_count += 1
        self.total_latency_ms += duration_ms
        if status_code >= 500:
            self.error_5xx_count += 1

    def get_summary(self) -> dict[str, Any]:
        import time

        uptime_s = round(time.time() - self.start_time, 2)
        avg_latency = round(self.total_latency_ms / max(1, self.request_count), 2)
        rate = round(self.request_count / max(1.0, uptime_s), 2)
        return {
            "uptime_seconds": uptime_s,
            "total_requests": self.request_count,
            "requests_per_second": rate,
            "average_latency_ms": avg_latency,
            "error_5xx_count": self.error_5xx_count,
            "error_5xx_rate": round(self.error_5xx_count / max(1, self.request_count), 4),
        }


metrics_collector = MetricsCollector()

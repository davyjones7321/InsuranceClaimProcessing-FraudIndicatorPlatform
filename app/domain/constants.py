"""Domain constants and fixed reason code allowlists."""

ALLOWED_REJECTION_REASON_CODES: frozenset[str] = frozenset(
    {
        "POLICY_NOT_FOUND",
        "POLICY_NOT_ACTIVE",
        "POLICY_OWNER_MISMATCH",
        "INCIDENT_OUTSIDE_POLICY_PERIOD",
        "INCIDENT_DATE_INVALID",
        "FILING_WINDOW_EXCEEDED",
        "NO_COVERAGE",
        "EXCLUDED",
        "WAITING_PERIOD_NOT_MET",
        "AMOUNT_INVALID",
        "LIMIT_EXCEEDED",
        "MISSING_FIELD",
        "DOCUMENTS_NOT_PROVIDED",
        "DOCUMENT_INVALID",
        "INVESTIGATION_CONCERNS",
        "SUSPECTED_MISREPRESENTATION",
        "VALIDATION_FAILURE",
        "OTHER",
    }
)

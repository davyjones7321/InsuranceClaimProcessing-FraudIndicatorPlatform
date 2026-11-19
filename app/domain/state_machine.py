"""Claim status state machine and allowed transitions."""

from app.domain.enums import ClaimStatus


class InvalidStateTransitionError(ValueError):
    """Raised when an illegal state transition is attempted."""

    def __init__(self, from_status: ClaimStatus, to_status: ClaimStatus):
        super().__init__(
            f"Illegal claim status transition from {from_status.value} to {to_status.value}"
        )
        self.from_status = from_status
        self.to_status = to_status


# Mechanically defined allowed transitions table
ALLOWED_TRANSITIONS: dict[ClaimStatus, frozenset[ClaimStatus]] = {
    ClaimStatus.DRAFT: frozenset(
        {
            ClaimStatus.REGISTERED,
        }
    ),
    ClaimStatus.REGISTERED: frozenset(
        {
            ClaimStatus.DOCUMENTS_PENDING,
            ClaimStatus.VALIDATION_FAILED,
            ClaimStatus.VALIDATED,
            ClaimStatus.WITHDRAWN,
        }
    ),
    ClaimStatus.DOCUMENTS_PENDING: frozenset(
        {
            ClaimStatus.REGISTERED,  # Docs supplied, revalidate
            ClaimStatus.REJECTED,  # Section G.77: human rejection with mandatory remarks
            ClaimStatus.WITHDRAWN,
        }
    ),
    ClaimStatus.VALIDATION_FAILED: frozenset(
        {
            ClaimStatus.REGISTERED,  # Corrected by authorized user
            ClaimStatus.REJECTED,  # Decision to reject
        }
    ),
    ClaimStatus.VALIDATED: frozenset(
        {
            ClaimStatus.RISK_EVALUATED,
        }
    ),
    ClaimStatus.RISK_EVALUATED: frozenset(
        {
            ClaimStatus.UNDER_INVESTIGATION,  # Score >= threshold
            ClaimStatus.READY_FOR_ASSESSMENT,  # Clean / not flagged
        }
    ),
    ClaimStatus.UNDER_INVESTIGATION: frozenset(
        {
            ClaimStatus.INVESTIGATION_COMPLETE,
            ClaimStatus.ESCALATED,
        }
    ),
    ClaimStatus.ESCALATED: frozenset(
        {
            ClaimStatus.INVESTIGATION_COMPLETE,  # Manager resolves
        }
    ),
    ClaimStatus.INVESTIGATION_COMPLETE: frozenset(
        {
            ClaimStatus.READY_FOR_ASSESSMENT,
        }
    ),
    ClaimStatus.READY_FOR_ASSESSMENT: frozenset(
        {
            ClaimStatus.ASSESSED,
            ClaimStatus.REJECTED,  # Section G.78
            ClaimStatus.INFO_REQUESTED,  # Section G.78
        }
    ),
    ClaimStatus.ASSESSED: frozenset(
        {
            ClaimStatus.APPROVED,
            ClaimStatus.REJECTED,
            ClaimStatus.INFO_REQUESTED,
        }
    ),
    ClaimStatus.INFO_REQUESTED: frozenset(
        {
            ClaimStatus.REGISTERED,  # Response received, full re-evaluation
            ClaimStatus.WITHDRAWN,
        }
    ),
    # Final states: no outgoing transitions by default
    ClaimStatus.APPROVED: frozenset(),
    ClaimStatus.REJECTED: frozenset(),
    ClaimStatus.WITHDRAWN: frozenset(),
}

FINAL_STATES: frozenset[ClaimStatus] = frozenset(
    {
        ClaimStatus.APPROVED,
        ClaimStatus.REJECTED,
        ClaimStatus.WITHDRAWN,
    }
)


def validate_transition(from_status: ClaimStatus, to_status: ClaimStatus) -> None:
    """Validate whether transitioning from from_status to to_status is allowed."""
    allowed_targets = ALLOWED_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed_targets:
        raise InvalidStateTransitionError(from_status, to_status)

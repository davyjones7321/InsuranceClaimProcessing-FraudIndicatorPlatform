"""Exhaustive test of the claim status state machine."""

import pytest

from app.domain.enums import ClaimStatus
from app.domain.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidStateTransitionError,
    validate_transition,
)


def test_exhaustive_state_transitions() -> None:
    """Generate all possible (from_status, to_status) pairs and assert only allowed ones succeed."""
    all_statuses = list(ClaimStatus)
    tested_pairs = 0
    allowed_count = 0
    rejected_count = 0

    for from_status in all_statuses:
        allowed_targets = ALLOWED_TRANSITIONS.get(from_status, frozenset())
        for to_status in all_statuses:
            tested_pairs += 1
            if to_status in allowed_targets:
                # Must succeed without error
                validate_transition(from_status, to_status)
                allowed_count += 1
            else:
                # Must raise InvalidStateTransitionError
                with pytest.raises(InvalidStateTransitionError):
                    validate_transition(from_status, to_status)
                rejected_count += 1

    total_possible = len(all_statuses) * len(all_statuses)
    assert tested_pairs == total_possible
    assert allowed_count + rejected_count == total_possible
    # Verify expected allowed transitions count
    expected_allowed = sum(len(targets) for targets in ALLOWED_TRANSITIONS.values())
    assert allowed_count == expected_allowed


def test_final_states_outgoing_transitions() -> None:
    """WITHDRAWN has zero outgoing transitions; APPROVED and REJECTED only allow reopen to READY_FOR_ASSESSMENT."""
    assert len(ALLOWED_TRANSITIONS.get(ClaimStatus.WITHDRAWN, frozenset())) == 0
    assert ALLOWED_TRANSITIONS.get(ClaimStatus.APPROVED, frozenset()) == frozenset(
        {ClaimStatus.READY_FOR_ASSESSMENT}
    )
    assert ALLOWED_TRANSITIONS.get(ClaimStatus.REJECTED, frozenset()) == frozenset(
        {ClaimStatus.READY_FOR_ASSESSMENT}
    )

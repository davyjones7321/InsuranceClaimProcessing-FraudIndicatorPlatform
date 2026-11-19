---
name: verify-invariants
description: "Verify mechanical invariants: State machine transition legality and HMAC-SHA256 audit chain integrity."
---

# Verify Invariants

Mechanically proves two core invariants across the platform:
1. **Claim Status State Machine (Section 8)**: Zero unlisted state transitions succeed; final states (APPROVED, REJECTED, WITHDRAWN) have zero outgoing transitions.
2. **Audit Log HMAC-SHA256 Chains (Section C)**: Independent chains per claim have no sequence gaps, no prev_hash breaks, no float corruption, and strict PII exclusion.

## Execution

```bash
pytest tests/unit/test_state_machine.py tests/integration/test_audit.py -v
```

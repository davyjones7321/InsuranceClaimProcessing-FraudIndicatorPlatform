---
name: verify-claim-platform
description: "Verify running health, API permissions, master data CRUD, claim registration, and document uploads for the claim platform."
---

# Verify Claim Platform

Doctor and verification suite for the Insurance Claim Processing Platform.

## Commands

### 1. Run Complete Test Suite
```bash
pytest -v
```

### 2. Verify Linting and Typing
```bash
ruff check .
mypy app tests
lint-imports
```

### 3. Verify Health Endpoints
- Probe `/healthz` -> HTTP 200 `{"status": "ok"}`
- Probe `/readyz` -> HTTP 200 `{"status": "ready"}`

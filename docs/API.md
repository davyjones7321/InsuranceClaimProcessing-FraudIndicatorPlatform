# API Specification & Route Permissions

Every API route requires server-side permission declarations. Default access is strictly default-deny.

| Endpoint | Method | Required Roles | Description |
|---|---|---|---|
| `/healthz` | GET | Public | Process liveness probe |
| `/readyz` | GET | Public | Database and ruleset readiness probe |
| `/api/auth/login` | POST | Public (Rate Limited) | Authenticate user, issue session cookie and CSRF token |
| `/api/auth/logout` | POST | Authenticated | Destroy active session and invalidate cookie |
| `/api/auth/me` | GET | Authenticated | Retrieve current user profile and role |
| `/api/customers` | GET | `staff`, `manager`, `admin` | Search and list customers |
| `/api/customers/{id}` | GET | `customer` (own), `staff`, `manager`, `admin` | View customer profile |
| `/api/claims` | POST | `customer`, `staff` | Register new claim |
| `/api/claims/{id}` | GET | Object-authorized | View claim details (enforces object ownership) |
| `/api/claims/{id}/documents` | POST | `customer` (own), `staff` | Upload claim document |
| `/api/claims/{id}/documents/{doc_id}` | GET | Object-authorized | Access claim document (sensitive docs restricted) |
| `/api/claims/{id}/assessment` | POST | `assessor` | Record assessment calculation |
| `/api/claims/{id}/decision` | POST | `manager`, `assessor` (amount-restricted) | Record claim decision |
| `/api/analytics/dashboard` | GET | `manager`, `admin` | View operations dashboard |

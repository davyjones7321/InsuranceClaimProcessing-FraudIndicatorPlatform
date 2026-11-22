"""FastAPI application entrypoint and middleware configuration."""

import time
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.analytics import router as analytics_router
from app.api.auth import router as auth_router
from app.api.claims import router as claims_router
from app.api.coverages import router as coverages_router
from app.api.customers import router as customers_router
from app.api.health import router as health_router
from app.api.incident_groups import router as incident_groups_router
from app.api.investigations import router as investigations_router
from app.api.master_data_import import router as master_data_import_router
from app.api.policies import router as policies_router
from app.api.required_documents import router as required_documents_router
from app.config import settings
from app.observability import logger, metrics_collector
from app.security.csrf import validate_csrf_token
from app.web.routes import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle events."""
    # 1. Validate production configuration (Phase P8 Task 4)
    if settings.ENV == "production":
        if settings.DEBUG:
            raise RuntimeError("DEBUG must not be True in production environment")
        if len(settings.SECRET_KEY) < 32 or "dev-" in settings.SECRET_KEY:
            raise RuntimeError(
                "Production SECRET_KEY must be a strong secret of at least 32 characters"
            )
        if len(settings.AUDIT_HMAC_KEY) < 32 or "dev-" in settings.AUDIT_HMAC_KEY:
            raise RuntimeError(
                "Production AUDIT_HMAC_KEY must be a strong secret of at least 32 characters"
            )

    # 2. Validate ruleset hash and schema
    try:
        from app.rules.loader import (
            ACTIVE_RULESET_SHA256,
            DEFAULT_RULESET_PATH,
            load_ruleset,
        )

        _, disk_sha256 = load_ruleset(DEFAULT_RULESET_PATH)
        if disk_sha256 != ACTIVE_RULESET_SHA256:
            raise RuntimeError(
                f"Active ruleset SHA-256 on disk ('{disk_sha256}') does not match registered hash ('{ACTIVE_RULESET_SHA256}')"
            )
    except Exception as exc:
        raise RuntimeError(f"Ruleset boot validation failed: {exc}") from exc

    # 3. Refuse to start if configured business timezone differs from recorded timezone in DB
    try:
        from sqlalchemy import select

        from app.db.models import Claim
        from app.db.session import get_db

        db_dep = app.dependency_overrides.get(get_db, get_db)
        db_gen = db_dep()
        db = next(db_gen)
        try:
            first_claim = db.scalars(select(Claim).limit(1)).first()
            if first_claim and first_claim.registered_tz != settings.BUSINESS_TIMEZONE:
                raise RuntimeError(
                    f"Configured business timezone '{settings.BUSINESS_TIMEZONE}' does not match "
                    f"recorded claim timezone '{first_claim.registered_tz}' in database"
                )
        finally:
            try:
                next(db_gen)
            except StopIteration:
                pass
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        # During offline migrations or fresh setups, tables may not yet exist
        pass

    yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs" if settings.ENV != "production" else None,
    redoc_url="/redoc" if settings.ENV != "production" else None,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    """Assign X-Request-ID and emit structured JSON access logs."""
    start_time = time.perf_counter()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    response: Response = await call_next(request)

    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
    response.headers["X-Request-ID"] = request_id

    client_ip = request.client.host if request.client else "unknown"
    extra_record = {
        "method": request.method,
        "path": request.url.path,
        "status_code": response.status_code,
        "duration_ms": duration_ms,
        "client_ip": client_ip,
    }
    logger.info(
        f"{request.method} {request.url.path} -> {response.status_code} ({duration_ms}ms)",
        extra={"request_id": request_id, "extra_fields": extra_record},
    )
    metrics_collector.record_request(duration_ms=duration_ms, status_code=response.status_code)
    return response


@app.middleware("http")
async def csrf_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    """Validate CSRF tokens on state-changing cookie-authenticated requests (P1 task 7)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        path = request.url.path
        if (
            not path.startswith("/api/auth/login")
            and not path.startswith("/ui")
            and not path.startswith("/health")
            and not path.startswith("/ready")
            and not path.startswith("/metrics")
        ):
            session_cookie = request.cookies.get(settings.SESSION_COOKIE_NAME)
            if session_cookie:
                csrf_token = request.headers.get("X-CSRF-Token")
                if not csrf_token or not validate_csrf_token(session_cookie, csrf_token):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": "Missing or invalid CSRF token for state-changing request"
                        },
                    )
    response: Response = await call_next(request)
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    """Inject security headers on all HTTP responses."""
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none';"
    )
    if settings.ENV in ("staging", "production"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Register API routers
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(customers_router)
app.include_router(policies_router)
app.include_router(coverages_router)
app.include_router(required_documents_router)
app.include_router(master_data_import_router)
app.include_router(claims_router)
app.include_router(incident_groups_router)
app.include_router(investigations_router)
app.include_router(analytics_router)

# Register Web UI router
app.include_router(web_router)


@app.get("/", include_in_schema=False)
def root_redirect() -> Response:
    """Redirect root path to web claims UI."""
    return RedirectResponse(url="/ui/claims", status_code=303)

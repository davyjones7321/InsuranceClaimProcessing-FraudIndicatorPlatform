"""FastAPI application entrypoint and middleware configuration."""

import time
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.api.analytics import router as analytics_router
from app.api.auth import router as auth_router
from app.api.claims import router as claims_router
from app.api.coverages import router as coverages_router
from app.api.customers import router as customers_router
from app.api.health import router as health_router
from app.api.master_data_import import router as master_data_import_router
from app.api.policies import router as policies_router
from app.api.required_documents import router as required_documents_router
from app.config import settings
from app.observability import logger
from app.security.csrf import validate_csrf_token


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle events."""
    # Validate startup configuration
    if settings.ENV == "production" and settings.DEBUG:
        raise RuntimeError("DEBUG must not be True in production environment")
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
    return response


@app.middleware("http")
async def csrf_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    """Validate CSRF tokens on state-changing cookie-authenticated requests (P1 task 7)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        path = request.url.path
        if (
            not path.startswith("/api/auth/login")
            and not path.startswith("/health")
            and not path.startswith("/ready")
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
        "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none';"
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
app.include_router(analytics_router)

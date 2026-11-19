"""FastAPI application entrypoint and middleware configuration."""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response

from app.api.auth import router as auth_router
from app.api.claims import router as claims_router
from app.api.coverages import router as coverages_router
from app.api.customers import router as customers_router
from app.api.health import router as health_router
from app.api.master_data_import import router as master_data_import_router
from app.api.policies import router as policies_router
from app.api.required_documents import router as required_documents_router
from app.config import settings


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

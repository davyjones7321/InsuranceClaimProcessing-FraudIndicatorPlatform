"""Mechanical enforcement test: verify that all routes declare explicit permissions."""

from fastapi.routing import APIRoute

from app.main import app
from app.security.rbac import RoleChecker, get_current_user

# Allowlist of public endpoints that do not require authenticated RBAC
PUBLIC_ALLOWED_ROUTES: set[tuple[str, str]] = {
    ("/", "GET"),
    ("/healthz", "GET"),
    ("/readyz", "GET"),
    ("/metrics", "GET"),
    ("/api/auth/login", "POST"),
    ("/docs", "GET"),
    ("/openapi.json", "GET"),
    ("/redoc", "GET"),
}


def test_every_route_declares_permission_or_is_explicitly_public() -> None:
    """Every registered FastAPI route must declare an explicit auth/role dependency, default-deny."""
    unprotected_routes: list[str] = []

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue

        path = route.path
        for method in route.methods:
            if (path, method) in PUBLIC_ALLOWED_ROUTES:
                continue

            # Check if route or endpoint depends on get_current_user or RoleChecker
            has_auth = False
            all_dependencies = route.dependencies + route.dependant.dependencies

            for dep in all_dependencies:
                call_target = dep.call
                if call_target is get_current_user:
                    has_auth = True
                    break
                if isinstance(call_target, RoleChecker):
                    has_auth = True
                    break
                # Check nested dependencies
                for sub_dep in dep.dependencies:
                    if sub_dep.call is get_current_user or isinstance(sub_dep.call, RoleChecker):
                        has_auth = True
                        break

            if not has_auth:
                unprotected_routes.append(f"{method} {path}")

    assert not unprotected_routes, (
        f"Security violation: The following routes lack explicit permission declarations: {unprotected_routes}"
    )

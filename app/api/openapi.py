"""Swagger / OpenAPI configuration for projectX-finance."""

from __future__ import annotations

from fastapi import FastAPI

API_VERSION = "0.1.0"
API_DESCRIPTION = (
    "Finance: invoices (fixed milestone/completion + monthly T&M), reserve, VAT, costs, "
    "invoice agenda, and **GET /reports/summary** management reporting."
)

OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "finance", "description": "Billing, invoices, reserve, reports"},
]

PUBLIC_PATHS = {"/health", "/brand", "/auth/login", "/auth/register"}


def configure_openapi(app: FastAPI) -> None:
    app.swagger_ui_parameters = {
        "persistAuthorization": True,
        "displayRequestDuration": True,
    }

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        _apply_bearer_auth(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def _apply_bearer_auth(schema: dict) -> None:
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "JWT from projectX-identity POST /auth/login",
    }
    schemes.pop("HTTPBearer", None)
    for path, methods in schema.get("paths", {}).items():
        if path in PUBLIC_PATHS:
            continue
        for op in methods.values():
            if not isinstance(op, dict):
                continue
            sec = op.get("security")
            if sec:
                op["security"] = [
                    {"BearerAuth": item.get("BearerAuth", item.get("HTTPBearer", []))}
                    if ("HTTPBearer" in item or "BearerAuth" in item)
                    else item
                    for item in sec
                ]
            else:
                op["security"] = [{"BearerAuth": []}]

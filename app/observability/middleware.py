"""HTTP request audit middleware (ECS access events)."""

from __future__ import annotations

import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.observability.audit import audit


def _principal_from_request(request: Request) -> tuple[str | None, str | None, str | None]:
    """Best-effort JWT decode for audit fields (never raises)."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None, None, None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None, None, None
    try:
        from jose import jwt

        from app.core.config import get_settings

        settings = get_settings()
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )
        return (
            str(payload.get("sub") or "") or None,
            str(payload.get("email") or "") or None,
            str(payload.get("tenant_id") or "") or None,
        )
    except Exception:
        return None, None, None


class RequestAuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in {"/health", "/docs", "/openapi.json", "/redoc"}:
            return await call_next(request)

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            user_id, email, tenant_id = _principal_from_request(request)
            outcome = "success" if status_code < 400 else "failure"
            audit(
                "http_request",
                outcome=outcome,
                category=["web", "api"],
                event_type=["access"],
                message=f"{request.method} {request.url.path} {status_code}",
                **{
                    "http.request.method": request.method,
                    "url.path": request.url.path,
                    "url.query": request.url.query or None,
                    "http.response.status_code": status_code,
                    "event.duration": int(duration_ms * 1_000_000),
                    "user.id": user_id,
                    "user.email": email,
                    "organization.id": tenant_id,
                    "client.ip": request.client.host if request.client else None,
                    "user_agent.original": request.headers.get("user-agent"),
                },
            )

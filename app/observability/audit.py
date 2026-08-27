"""Uniform security / activity audit helper (ECS event.* fields).

Canonical copy lives in projectX-identity; keep other services in sync.

Envelope (every event):
  @timestamp, message, ecs.version, event.*, service.*,
  user.id / user.email, organization.id, session.id, trace.id / span.id
  plus entity fields: customer.id, project.id, invoice.id, time_entry.id, resource.id, …
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Sequence

from opentelemetry import trace

_log = logging.getLogger("projectx.audit")

# JWT jti (or equivalent) bound for the current request by middleware / deps.
_session_id: ContextVar[str | None] = ContextVar("audit_session_id", default=None)

# Legacy labels.* → uniform entity fields (call sites may still pass either).
_FIELD_ALIASES = {
    "labels.customer_id": "customer.id",
    "labels.customer_name": "customer.name",
    "labels.invoice_id": "invoice.id",
    "labels.invoice_kind": "invoice.kind",
    "labels.period": "invoice.period",
    "labels.time_entry_id": "time_entry.id",
    "labels.partner_id": "user.target.id",
    "labels.hours": "time_entry.hours",
    "labels.resource_id": "resource.id",
    "labels.display_name": "resource.name",
    "labels.funnel_status": "project.funnel_status",
    "labels.service_id": "service.id",
    "labels.version": "service.version",
    "labels.rate_old": "resource.rate.old",
    "labels.rate_new": "resource.rate.new",
    "labels.appointment_id": "appointment.id",
    "labels.kind": "appointment.kind",
}


def set_audit_session_id(session_id: str | None) -> None:
    _session_id.set(session_id)


def get_audit_session_id() -> str | None:
    return _session_id.get()


def _trace_ids() -> tuple[str | None, str | None]:
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        pass
    return None, None


def _ecs_key(key: str) -> str:
    if "." in key:
        return _FIELD_ALIASES.get(key, key)
    dotted = key.replace("_", ".")
    return _FIELD_ALIASES.get(dotted, dotted)


def build_ecs_doc(
    action: str,
    *,
    outcome: str,
    category: str | Sequence[str] = "api",
    event_type: str | Sequence[str] | None = None,
    message: str | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    cats = [category] if isinstance(category, str) else list(category)
    if event_type is None:
        types = ["info"] if outcome == "success" else ["error" if outcome == "failure" else "info"]
    elif isinstance(event_type, str):
        types = [event_type]
    else:
        types = list(event_type)

    trace_id, span_id = _trace_ids()
    doc: dict[str, Any] = {
        "@timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "log.level": "info",
        "message": message or action,
        "ecs.version": "8.11.0",
        "event.action": action,
        "event.outcome": outcome,
        "event.category": cats,
        "event.type": types,
        "event.kind": "event",
    }
    if service_name:
        doc["service.name"] = service_name
    if environment:
        doc["service.environment"] = environment
    if trace_id:
        doc["trace.id"] = trace_id
    if span_id:
        doc["span.id"] = span_id

    session = fields.pop("session.id", None) or fields.pop("session_id", None) or get_audit_session_id()
    if session:
        doc["session.id"] = str(session)

    for key, value in fields.items():
        if value is None:
            continue
        doc[_ecs_key(key)] = value
    return doc


def actor_fields(
    *,
    user_id: str | None = None,
    email: str | None = None,
    tenant_id: str | None = None,
    session_id: str | None = None,
    name: str | None = None,
    roles: list[str] | str | None = None,
) -> dict[str, Any]:
    """Standard who/tenant/session fields for domain audits."""
    out: dict[str, Any] = {}
    if user_id:
        out["user.id"] = user_id
    if email:
        out["user.email"] = email
    if name:
        out["user.name"] = name
    if roles is not None:
        out["user.roles"] = [roles] if isinstance(roles, str) else list(roles)
    if tenant_id:
        out["organization.id"] = tenant_id
    if session_id:
        out["session.id"] = session_id
    return out


def audit(
    action: str,
    *,
    outcome: str,
    category: str | Sequence[str] = "api",
    event_type: str | Sequence[str] | None = None,
    message: str | None = None,
    **fields: Any,
) -> None:
    """Emit an ECS audit event as a single JSON log line (stdout + OTLP-safe)."""
    try:
        from app.core.config import get_settings

        settings = get_settings()
        service_name = settings.service_name
        environment = settings.environment
    except Exception:
        service_name = None
        environment = None

    doc = build_ecs_doc(
        action,
        outcome=outcome,
        category=category,
        event_type=event_type,
        message=message,
        service_name=service_name,
        environment=environment,
        **fields,
    )
    if service_name:
        doc["event.dataset"] = f"{service_name}.audit"
    _log.info("%s", json.dumps(doc, default=str, ensure_ascii=False))

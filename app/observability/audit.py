"""Security / activity audit helper (ECS event.* fields)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from opentelemetry import trace

_log = logging.getLogger("projectx.audit")


def _trace_ids() -> tuple[str | None, str | None]:
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        pass
    return None, None


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
    for key, value in fields.items():
        if value is None:
            continue
        ecs_key = key.replace("_", ".") if "." not in key else key
        doc[ecs_key] = value
    return doc


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
    # Body is pure ECS JSON — no nested LogRecord extras (OTel rejects dict attrs).
    _log.info("%s", json.dumps(doc, default=str, ensure_ascii=False))

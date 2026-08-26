"""ECS-shaped JSON logging for stdout (and OTLP bridge)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

_PRIMITIVE_ATTR_TYPES = (bool, str, bytes, int, float)


def _trace_ids() -> tuple[str | None, str | None]:
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        pass
    return None, None


def _otel_safe_attr(value: Any) -> Any | None:
    """Coerce a value to an OTel-accepted attribute type, or None to skip."""
    if value is None:
        return None
    if isinstance(value, _PRIMITIVE_ATTR_TYPES):
        return value
    if isinstance(value, (list, tuple)):
        if all(isinstance(x, _PRIMITIVE_ATTR_TYPES) for x in value):
            return list(value)
        return [str(x) for x in value]
    if isinstance(value, dict):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


class FlattenEcsForOtelFilter(logging.Filter):
    """Flatten nested ``record.ecs`` into primitive LogRecord attributes for OTel.

    OpenTelemetry's ``LoggingHandler`` exports non-standard ``LogRecord`` attributes
    as OTLP attributes. A nested ``ecs`` dict is invalid (``Invalid type dict for
    attribute 'ecs'``). This filter expands ``ecs`` into individual primitive fields
    (ECS dotted keys are fine as ``__dict__`` keys) and removes ``ecs``.

    Attach **only** to the OTel ``LoggingHandler``. The stdout ``StreamHandler`` must
    remain first in ``root.handlers`` so ``EcsJsonFormatter`` still sees nested ``ecs``
    before this filter mutates the shared record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ecs = getattr(record, "ecs", None)
        if not isinstance(ecs, dict):
            return True
        for key, value in ecs.items():
            safe = _otel_safe_attr(value)
            if safe is not None:
                record.__dict__[key] = safe
        try:
            delattr(record, "ecs")
        except AttributeError:
            record.__dict__.pop("ecs", None)
        return True


class EcsJsonFormatter(logging.Formatter):
    """Emit one ECS-compatible JSON object per log line."""

    def __init__(self, *, service_name: str, environment: str, event_dataset: str) -> None:
        super().__init__()
        self.service_name = service_name
        self.environment = environment
        self.event_dataset = event_dataset

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = _trace_ids()
        doc: dict[str, Any] = {
            "@timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "log.level": record.levelname.lower(),
            "message": record.getMessage(),
            "service.name": self.service_name,
            "service.environment": self.environment,
            "event.dataset": self.event_dataset,
            "ecs.version": "8.11.0",
        }
        if trace_id:
            doc["trace.id"] = trace_id
        if span_id:
            doc["span.id"] = span_id
        if record.exc_info:
            doc["error.message"] = self.formatException(record.exc_info)

        # Flatten ECS extras attached via logger.info(..., extra={"ecs": {...}})
        ecs = getattr(record, "ecs", None)
        if isinstance(ecs, dict):
            for key, value in ecs.items():
                if value is not None:
                    doc[key] = value

        return json.dumps(doc, default=str, ensure_ascii=False)


def configure_ecs_logging(*, service_name: str, environment: str) -> None:
    """Replace root handlers with a single ECS JSON stream handler."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(
        EcsJsonFormatter(
            service_name=service_name,
            environment=environment,
            event_dataset=f"{service_name}.app",
        )
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # Quiet noisy access loggers; request audit middleware covers HTTP.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

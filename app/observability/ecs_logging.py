"""ECS-shaped JSON logging for stdout (and OTLP bridge)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


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


class EcsJsonFormatter(logging.Formatter):
    """Emit one ECS-compatible JSON object per log line."""

    def __init__(self, *, service_name: str, environment: str, event_dataset: str) -> None:
        super().__init__()
        self.service_name = service_name
        self.environment = environment
        self.event_dataset = event_dataset

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        # Audit helper already emits a full ECS document as the message.
        if msg.startswith("{") and '"@timestamp"' in msg:
            try:
                json.loads(msg)
                return msg
            except Exception:
                pass

        trace_id, span_id = _trace_ids()
        doc: dict[str, Any] = {
            "@timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "log.level": record.levelname.lower(),
            "message": msg,
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

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

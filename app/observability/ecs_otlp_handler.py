"""Map ECS JSON log lines to structured OTLP log attributes (not raw JSON body)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from opentelemetry.sdk._logs import LogRecord as OtlpLogRecord
from opentelemetry.sdk._logs import LoggingHandler


def _coerce_attribute(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        if not value:
            return []
        if all(isinstance(x, str) for x in value):
            return list(value)
        if all(isinstance(x, bool) for x in value):
            return list(value)
        if all(isinstance(x, int) for x in value):
            return list(value)
        if all(isinstance(x, float) for x in value):
            return list(value)
        return json.dumps(value, default=str, ensure_ascii=False)
    if isinstance(value, dict):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def _parse_ecs_message(msg: str) -> tuple[str, dict[str, Any], int | None] | None:
    if not msg.startswith("{") or "@timestamp" not in msg:
        return None
    try:
        doc = json.loads(msg)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None

    body = doc.pop("message", msg)
    if not isinstance(body, str):
        body = str(body)

    timestamp_ns: int | None = None
    ts_raw = doc.pop("@timestamp", None)
    if isinstance(ts_raw, str):
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            timestamp_ns = int(ts.timestamp() * 1e9)
        except Exception:
            pass

    attrs: dict[str, Any] = {}
    for key, value in doc.items():
        coerced = _coerce_attribute(value)
        if coerced is not None:
            attrs[key] = coerced
    return body, attrs, timestamp_ns


class EcsOtlpLoggingHandler(LoggingHandler):
    """OTLP handler: ECS JSON messages become attributes, not a nested JSON body."""

    def _translate(self, record: logging.LogRecord) -> OtlpLogRecord:
        otlp_record = super()._translate(record)
        parsed = _parse_ecs_message(record.getMessage())
        if not parsed:
            return otlp_record

        body, ecs_attrs, timestamp_ns = parsed
        merged = dict(otlp_record.attributes or {})
        merged.update(ecs_attrs)

        severity_text = otlp_record.severity_text
        level = ecs_attrs.get("log.level")
        if isinstance(level, str) and level:
            severity_text = "WARN" if level == "warning" else level.upper()

        return OtlpLogRecord(
            timestamp=timestamp_ns if timestamp_ns is not None else otlp_record.timestamp,
            observed_timestamp=otlp_record.observed_timestamp,
            trace_id=otlp_record.trace_id,
            span_id=otlp_record.span_id,
            trace_flags=otlp_record.trace_flags,
            severity_text=severity_text,
            severity_number=otlp_record.severity_number,
            body=body,
            resource=otlp_record.resource,
            attributes=merged,
        )

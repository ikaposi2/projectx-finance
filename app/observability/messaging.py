"""NATS JetStream trace propagation for Elastic service map edges."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind

_tracer = trace.get_tracer(__name__)
_TRACE_KEY = "_tracecontext"


def inject_into_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    carrier: dict[str, str] = {}
    inject(carrier)
    if carrier:
        envelope[_TRACE_KEY] = carrier
    return envelope


def attach_consumer_context(envelope: dict[str, Any]) -> otel_context.Context:
    carrier = envelope.get(_TRACE_KEY)
    if isinstance(carrier, dict):
        return extract(carrier)
    return otel_context.get_current()


@contextmanager
def nats_publish_span(*, subject: str, event_type: str) -> Iterator[None]:
    with _tracer.start_as_current_span(
        f"publish {subject}",
        kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "nats",
            "messaging.destination.name": subject,
            "messaging.operation": "publish",
            "messaging.destination.kind": "topic",
            "event.type": event_type,
            "peer.service": "nats",
        },
    ):
        yield


@contextmanager
def nats_consume_span(*, subject: str, event_type: str) -> Iterator[None]:
    with _tracer.start_as_current_span(
        f"process {subject}",
        kind=SpanKind.CONSUMER,
        attributes={
            "messaging.system": "nats",
            "messaging.destination.name": subject,
            "messaging.operation": "process",
            "event.type": event_type or "unknown",
            "peer.service": "nats",
        },
    ):
        yield

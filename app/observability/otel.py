"""OpenTelemetry traces + logs setup with ECS stdout logging."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI

from app.core.config import get_settings
from app.observability.ecs_logging import configure_ecs_logging
from app.observability.middleware import RequestAuditMiddleware

logger = logging.getLogger(__name__)


def _ecs_line(**fields: object) -> str:
    doc = {
        "@timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ecs.version": "8.11.0",
        **fields,
    }
    return json.dumps(doc, default=str, ensure_ascii=False)


def setup_observability(app: FastAPI) -> None:
    settings = get_settings()
    configure_ecs_logging(
        service_name=settings.service_name,
        environment=settings.environment,
    )

    app.add_middleware(RequestAuditMiddleware)

    endpoint = (settings.otel_exporter_otlp_endpoint or "").rstrip("/")
    if not endpoint:
        logger.info(
            "%s",
            _ecs_line(
                message="otel_disabled",
                **{
                    "service.name": settings.service_name,
                    "event.action": "otel_setup",
                    "event.outcome": "success",
                },
            ),
        )
        return

    from opentelemetry import trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    HTTPXClientInstrumentor = None
    try:
        import httpx  # noqa: F401

        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:
        pass
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    from app.observability.ecs_otlp_handler import EcsOtlpLoggingHandler

    set_global_textmap(TraceContextTextMapPropagator())

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "deployment.environment": settings.environment,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"))
    )
    set_logger_provider(logger_provider)

    LoggingInstrumentor().instrument(set_logging_format=False)
    otel_handler = EcsOtlpLoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    logging.getLogger().addHandler(otel_handler)

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="/health,/docs,/openapi.json,/redoc",
    )
    if HTTPXClientInstrumentor is not None:
        HTTPXClientInstrumentor().instrument()

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument()
    except Exception:
        pass

    logger.info(
        "%s",
        _ecs_line(
            message="otel_enabled",
            **{
                "service.name": settings.service_name,
                "service.environment": settings.environment,
                "event.action": "otel_setup",
                "event.outcome": "success",
                "url.full": endpoint,
            },
        ),
    )

import asyncio
import json
import logging
from typing import Any

import nats
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import RetentionPolicy, StorageType, StreamConfig

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.observability.messaging import attach_consumer_context, nats_consume_span
from app.services.ledger import apply_time_approval, reverse_time_effect

from opentelemetry import context as otel_context

logger = logging.getLogger(__name__)
settings = get_settings()

APPROVED = "com.projectx.time.TimeEntryApproved.v1"
REFUSED = "com.projectx.time.TimeEntryRefused.v1"
RESET = "com.projectx.time.TimeEntryReset.v1"


async def handle_envelope(envelope: dict[str, Any]) -> None:
    event_type = envelope.get("type")
    data = envelope.get("data") or {}
    tenant_id = str(envelope.get("tenantid") or "")
    if not tenant_id or not isinstance(data, dict):
        logger.warning("skip event missing tenant/data type=%s", event_type)
        return

    time_entry_id = str(data.get("time_entry_id") or "")
    if not time_entry_id:
        logger.warning("skip event missing time_entry_id type=%s", event_type)
        return

    async with SessionLocal() as db:
        if event_type == APPROVED:
            classification = str(data.get("classification") or "")
            if classification not in ("billable", "approved_non_billable"):
                return
            partner_id = data.get("partner_id")
            hours = data.get("hours")
            if not partner_id or hours is None:
                logger.warning("skip approve incomplete payload entry=%s", time_entry_id)
                return
            project_raw = data.get("project_id")
            project_id = str(project_raw) if project_raw else None
            rate_raw = data.get("rate_eur")
            rate_eur = float(rate_raw) if rate_raw is not None else None
            applied = await apply_time_approval(
                db,
                tenant_id=tenant_id,
                time_entry_id=time_entry_id,
                partner_id=str(partner_id),
                project_id=project_id,
                hours=float(hours),
                classification=classification,
                rate_eur=rate_eur,
            )
            if applied:
                logger.info(
                    "finance applied entry=%s class=%s hours=%s",
                    time_entry_id,
                    classification,
                    hours,
                )
            return

        if event_type in (REFUSED, RESET):
            reversed_ok = await reverse_time_effect(db, time_entry_id=time_entry_id)
            if reversed_ok:
                logger.info("finance reversed entry=%s type=%s", time_entry_id, event_type)
            return


async def run_consumer(stop: asyncio.Event) -> None:
    if not settings.nats_url.strip():
        logger.info("NATS_URL empty; finance time consumer disabled")
        await stop.wait()
        return

    while not stop.is_set():
        nc = None
        try:
            nc = await nats.connect(settings.nats_url)
            js = nc.jetstream()
            try:
                await js.add_stream(
                    StreamConfig(
                        name=settings.nats_stream,
                        subjects=["projectx.events.>"],
                        retention=RetentionPolicy.LIMITS,
                        storage=StorageType.FILE,
                        max_msgs=100_000,
                    )
                )
            except Exception:
                pass

            sub = await js.pull_subscribe(
                settings.nats_filter_subject,
                durable=settings.nats_consumer,
                stream=settings.nats_stream,
            )
            logger.info(
                "finance consumer listening durable=%s filter=%s",
                settings.nats_consumer,
                settings.nats_filter_subject,
            )

            while not stop.is_set():
                try:
                    msgs = await sub.fetch(batch=8, timeout=1)
                except NatsTimeoutError:
                    continue
                except Exception as exc:
                    logger.warning("fetch failed (%s); reconnecting", exc)
                    break

                for msg in msgs:
                    token = None
                    try:
                        envelope = json.loads(msg.data.decode("utf-8"))
                        ctx = attach_consumer_context(envelope)
                        token = otel_context.attach(ctx)
                        event_type = str(envelope.get("type") or "unknown")
                        with nats_consume_span(
                            subject=settings.nats_filter_subject,
                            event_type=event_type,
                        ):
                            await handle_envelope(envelope)
                        await msg.ack()
                    except Exception:
                        logger.exception("failed handling time event")
                        try:
                            await msg.nak()
                        except Exception:
                            pass
                    finally:
                        if token is not None:
                            otel_context.detach(token)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("finance consumer connection failed; retry in 5s")
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        finally:
            if nc is not None:
                try:
                    await nc.drain()
                except Exception:
                    try:
                        await nc.close()
                    except Exception:
                        pass

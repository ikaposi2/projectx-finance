"""Security / activity audit helper (ECS event.* fields)."""

from __future__ import annotations

import logging
from typing import Any, Sequence

_log = logging.getLogger("projectx.audit")


def audit(
    action: str,
    *,
    outcome: str,
    category: str | Sequence[str] = "api",
    event_type: str | Sequence[str] | None = None,
    message: str | None = None,
    **fields: Any,
) -> None:
    """Emit an ECS audit event via the application logger.

    ``fields`` should use ECS key names (e.g. ``user.id``, ``organization.id``).
    """
    cats = [category] if isinstance(category, str) else list(category)
    types: list[str]
    if event_type is None:
        types = ["info"] if outcome == "success" else ["error" if outcome == "failure" else "info"]
    elif isinstance(event_type, str):
        types = [event_type]
    else:
        types = list(event_type)

    ecs: dict[str, Any] = {
        "event.action": action,
        "event.outcome": outcome,
        "event.category": cats,
        "event.type": types,
        "event.kind": "event",
    }
    for key, value in fields.items():
        if value is not None:
            # Allow pythonic kwargs: user_id -> user.id
            ecs_key = key.replace("_", ".") if "." not in key else key
            ecs[ecs_key] = value

    _log.info(
        message or action,
        extra={"ecs": ecs},
    )

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import jwt

from app.core.config import get_settings

settings = get_settings()


def mint_service_access_token(*, user_id: str, tenant_id: str, role: str = "admin") -> str:
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role,
        "email": "finance-cron@internal",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

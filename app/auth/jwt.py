from dataclasses import dataclass

from jose import JWTError, jwt

from app.core.config import get_settings
from app.observability.audit import set_audit_session_id

settings = get_settings()


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    tenant_id: str
    role: str
    locale: str
    session_id: str | None = None


def decode_access_token(token: str) -> Principal:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("invalid_token") from exc
    sub = payload.get("sub")
    tenant_id = payload.get("tenant_id")
    if not sub or not tenant_id:
        raise ValueError("invalid_token")
    session_id = str(payload.get("jti") or "") or None
    # Bind for domain audit() calls in this request (middleware also sets this).
    set_audit_session_id(session_id)
    return Principal(
        user_id=str(sub),
        email=str(payload.get("email") or ""),
        tenant_id=str(tenant_id),
        role=str(payload.get("role") or "partner"),
        locale=str(payload.get("locale") or "nl"),
        session_id=session_id,
    )

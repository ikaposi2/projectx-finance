from dataclasses import dataclass

from jose import JWTError, jwt

from app.core.config import get_settings

settings = get_settings()


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    tenant_id: str
    role: str
    locale: str


def decode_access_token(token: str) -> Principal:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("invalid_token") from exc
    sub = payload.get("sub")
    tenant_id = payload.get("tenant_id")
    if not sub or not tenant_id:
        raise ValueError("invalid_token")
    return Principal(
        user_id=str(sub),
        email=str(payload.get("email") or ""),
        tenant_id=str(tenant_id),
        role=str(payload.get("role") or "partner"),
        locale=str(payload.get("locale") or "nl"),
    )

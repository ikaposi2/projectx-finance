import httpx

from app.core.config import get_settings

settings = get_settings()


class UpstreamError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


async def fetch_project(*, project_id: str, access_token: str) -> dict:
    url = f"{settings.project_service_url.rstrip('/')}/projects/{project_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise UpstreamError("project_unavailable") from exc
    if res.status_code == 404:
        raise UpstreamError("project_not_found")
    if res.status_code >= 400:
        raise UpstreamError("project_unavailable")
    return res.json()


async def fetch_bookable_projects(*, access_token: str) -> list[dict]:
    url = f"{settings.project_service_url.rstrip('/')}/projects/bookable"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise UpstreamError("project_unavailable") from exc
    if res.status_code >= 400:
        raise UpstreamError("project_unavailable")
    data = res.json()
    return data if isinstance(data, list) else []


async def fetch_customer(*, customer_id: str, access_token: str) -> dict | None:
    url = f"{settings.customer_service_url.rstrip('/')}/customers/{customer_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise UpstreamError("customer_unavailable") from exc
    if res.status_code == 404:
        return None
    if res.status_code >= 400:
        raise UpstreamError("customer_unavailable")
    return res.json()

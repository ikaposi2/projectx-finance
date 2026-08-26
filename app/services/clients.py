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


async def fetch_bookable_projects(*, access_token: str, include_complete: bool = False) -> list[dict]:
    url = f"{settings.project_service_url.rstrip('/')}/projects/bookable"
    if include_complete:
        url = f"{url}?include_complete=true"
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


async def fetch_user_names(*, access_token: str) -> dict[str, str]:
    """Map user id → full_name from identity directory."""
    url = f"{settings.identity_service_url.rstrip('/')}/users"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return {}
    if res.status_code >= 400:
        return {}
    data = res.json()
    if not isinstance(data, list):
        return {}
    return {str(u.get("id")): str(u.get("full_name") or u.get("email") or "") for u in data if u.get("id")}


async def refuse_time_entry(*, time_entry_id: str, access_token: str) -> None:
    url = f"{settings.time_service_url.rstrip('/')}/entries/{time_entry_id}/refuse"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        raise UpstreamError("time_unavailable") from exc
    if res.status_code == 404:
        raise UpstreamError("time_entry_not_found")
    if res.status_code == 409:
        raise UpstreamError("not_refusable")
    if res.status_code >= 400:
        raise UpstreamError("time_unavailable")


async def fetch_resources(*, access_token: str) -> list[dict]:
    url = f"{settings.partner_service_url.rstrip('/')}/resources"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise UpstreamError("partner_unavailable") from exc
    if res.status_code >= 400:
        raise UpstreamError("partner_unavailable")
    data = res.json()
    return data if isinstance(data, list) else []


async def fetch_time_entries(*, access_token: str, from_date: str, to_date: str) -> list[dict]:
    url = f"{settings.time_service_url.rstrip('/')}/entries"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(
                url,
                headers=headers,
                params={"from": from_date, "to": to_date},
            )
    except httpx.HTTPError as exc:
        raise UpstreamError("time_unavailable") from exc
    if res.status_code >= 400:
        raise UpstreamError("time_unavailable")
    data = res.json()
    return data if isinstance(data, list) else []


async def advance_project_funnel(
    *,
    project_id: str,
    funnel_status: str,
    access_token: str,
) -> None:
    """Best-effort project funnel update after invoice send/pay."""
    if not project_id:
        return
    url = f"{settings.project_service_url.rstrip('/')}/projects/{project_id}/funnel"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(
                url,
                headers=headers,
                json={"funnel_status": funnel_status},
            )
    except httpx.HTTPError as exc:
        raise UpstreamError("project_unavailable") from exc
    if res.status_code >= 400:
        raise UpstreamError(res.text or "project_funnel_failed")

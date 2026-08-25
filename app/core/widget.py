from urllib.parse import urlparse

from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.core.products import normalize_workspace_domain


def request_widget_origin(request: Request) -> str:
    header_origin = request.headers.get("x-widget-origin") or request.headers.get("origin") or ""
    if header_origin:
        return normalize_workspace_domain(header_origin)
    referer = request.headers.get("referer") or ""
    if referer:
        parsed = urlparse(referer)
        if parsed.scheme and parsed.hostname:
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{parsed.hostname.lower()}{port}"
    return ""


def is_local_development_origin(origin: str) -> bool:
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.hostname in {"localhost", "127.0.0.1"} and parsed.scheme in {"http", "https"}


def approved_widget_origins(product: dict) -> set[str]:
    values = product.get("approved_domains") or []
    return {normalize_workspace_domain(value) for value in values if value}


def validate_widget_origin(product: dict, request: Request) -> str:
    origin = request_widget_origin(request)
    approved = approved_widget_origins(product)
    if not origin:
        if settings.environment.lower() == "development":
            return ""
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Widget origin is required")
    if approved:
        if origin not in approved:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This website is not approved for this widget")
        return origin
    allowed_dev_origins = {normalize_workspace_domain(origin) for origin in settings.cors_origin_list}
    if origin in allowed_dev_origins or is_local_development_origin(origin):
        return origin
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No approved website domain is configured for this workspace")

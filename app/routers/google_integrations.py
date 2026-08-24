from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.core.security import get_current_user
from app.schemas import GoogleCalendarConnectOut, GoogleCalendarStatusOut
from app.services.google_calendar import google_calendar_service

router = APIRouter(prefix="/api/integrations/google", tags=["google calendar integration"])


def frontend_redirect(status_value: str, message: str = "") -> RedirectResponse:
    base_url = settings.resolved_frontend_url.rstrip("/")
    query = f"google_calendar={quote(status_value)}"
    if message:
        query += f"&google_calendar_message={quote(message)}"
    return RedirectResponse(f"{base_url}/dashboard?{query}")


@router.get("/connect", response_model=GoogleCalendarConnectOut)
async def connect_google_calendar(user: dict = Depends(get_current_user)) -> GoogleCalendarConnectOut:
    authorization_url = await google_calendar_service.create_authorization_url(user)
    return GoogleCalendarConnectOut(authorization_url=authorization_url)


@router.post("/reconnect", response_model=GoogleCalendarConnectOut)
async def reconnect_google_calendar(user: dict = Depends(get_current_user)) -> GoogleCalendarConnectOut:
    authorization_url = await google_calendar_service.create_authorization_url(user)
    return GoogleCalendarConnectOut(authorization_url=authorization_url)


@router.get("/callback")
async def google_calendar_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    if error:
        return frontend_redirect("error", "Google Calendar access was not approved")
    try:
        await google_calendar_service.complete_oauth_callback(code or "", state or "")
    except Exception as exc:
        detail = getattr(exc, "detail", "Google Calendar connection failed")
        return frontend_redirect("error", str(detail))
    return frontend_redirect("connected")


@router.get("/status", response_model=GoogleCalendarStatusOut)
async def google_calendar_status(user: dict = Depends(get_current_user)) -> GoogleCalendarStatusOut:
    return GoogleCalendarStatusOut(**await google_calendar_service.status_for_user(user))


@router.delete("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_google_calendar(user: dict = Depends(get_current_user)) -> None:
    await google_calendar_service.disconnect_user(user)

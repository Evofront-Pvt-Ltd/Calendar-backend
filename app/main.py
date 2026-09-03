from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import close_mongo_connection, connect_to_mongo
from app.routers import (
    auth,
    availability,
    bookings,
    contacts,
    dashboard,
    event_types,
    google_integrations,
    products,
    public,
    sendgrid_webhooks,
    widget,
)

logger = logging.getLogger(__name__)

MISSED_CALL_SCAN_INTERVAL_SECONDS = 60


def verify_signing_secret() -> None:
    if settings.environment.lower() in {"development", "test", "local"}:
        return
    if settings.jwt_secret_is_insecure:
        raise RuntimeError(
            "JWT_SECRET is missing, too short, or still set to a placeholder. "
            "Set a random secret of at least 32 characters before starting in this environment."
        )


async def _missed_call_scan_loop() -> None:
    from app.services.booking_claims import scan_all_missed_calls

    while True:
        try:
            marked = await scan_all_missed_calls()
            if marked:
                logger.info("Missed-call scan marked %s booking(s)", marked)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Missed-call scan failed")
        await asyncio.sleep(MISSED_CALL_SCAN_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    verify_signing_secret()
    await connect_to_mongo()
    scan_task = asyncio.create_task(_missed_call_scan_loop(), name="missed-call-scan")
    try:
        yield
    finally:
        scan_task.cancel()
        try:
            await scan_task
        except asyncio.CancelledError:
            pass
        await close_mongo_connection()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(products.router)
app.include_router(availability.router)
app.include_router(event_types.router)
app.include_router(bookings.router)
app.include_router(contacts.router)
app.include_router(dashboard.router)
app.include_router(google_integrations.router)
app.include_router(public.router)
app.include_router(widget.router)
app.include_router(sendgrid_webhooks.router)


@app.get("/")
async def root() -> dict:
    return {
        "service": settings.app_name,
        "environment": settings.environment,
        "status": "ok",
        "health": "/health",
        "docs": "/docs",
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "environment": settings.environment}

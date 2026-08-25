from contextlib import asynccontextmanager

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_mongo()
    yield
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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "environment": settings.environment}

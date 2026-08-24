from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.database import get_database
from app.core.products import product_context
from app.core.security import get_current_user
from app.core.utils import now_utc, object_id, public_doc, unique_slug
from app.schemas import EventTypeCreate, EventTypeOut, EventTypeUpdate

router = APIRouter(prefix="/api/event-types", tags=["event types"])


async def attach_public_path(event_type: dict, fallback_slug: str) -> dict:
    owner_slug = fallback_slug
    try:
        owner = await get_database().users.find_one({"_id": object_id(event_type["owner_id"])})
    except ValueError:
        owner = None
    if owner is not None:
        owner_slug = owner["slug"]
    event_type["owner_slug"] = owner_slug
    event_type["public_path"] = f"/book/{owner_slug}/{event_type['slug']}"
    return event_type


@router.get("", response_model=list[EventTypeOut])
async def list_event_types(
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> list[EventTypeOut]:
    context = await product_context(user, product_id, "view_product")
    cursor = get_database().event_types.find({"product_id": str(context.product["_id"])}).sort("created_at", -1)
    items = [EventTypeOut(**public_doc(await attach_public_path(item, user["slug"]))) async for item in cursor]
    return items


@router.post("", response_model=EventTypeOut, status_code=status.HTTP_201_CREATED)
async def create_event_type(
    payload: EventTypeCreate,
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> EventTypeOut:
    context = await product_context(user, product_id, "manage_event_types", require_active=True)
    db = get_database()
    slug = await unique_slug(db.event_types, payload.title, {"owner_id": str(user["_id"])})
    timestamp = now_utc()
    doc = {
        **payload.model_dump(),
        "product_id": str(context.product["_id"]),
        "owner_id": str(user["_id"]),
        "slug": slug,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = await db.event_types.insert_one(doc)
    doc["_id"] = result.inserted_id
    return EventTypeOut(**public_doc(await attach_public_path(doc, user["slug"])))


@router.get("/{event_type_id}", response_model=EventTypeOut)
async def get_event_type(
    event_type_id: str,
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> EventTypeOut:
    context = await product_context(user, product_id, "view_product")
    try:
        event_id = object_id(event_type_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event type not found") from None
    doc = await get_database().event_types.find_one({"_id": event_id, "product_id": str(context.product["_id"])})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event type not found")
    return EventTypeOut(**public_doc(await attach_public_path(doc, user["slug"])))


@router.patch("/{event_type_id}", response_model=EventTypeOut)
async def update_event_type(
    event_type_id: str,
    payload: EventTypeUpdate,
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> EventTypeOut:
    context = await product_context(user, product_id, "manage_event_types", require_active=True)
    try:
        event_id = object_id(event_type_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event type not found") from None
    updates = payload.model_dump(exclude_unset=True)
    if updates:
        updates["updated_at"] = now_utc()
        await get_database().event_types.update_one(
            {"_id": event_id, "product_id": str(context.product["_id"])},
            {"$set": updates},
        )
    doc = await get_database().event_types.find_one({"_id": event_id, "product_id": str(context.product["_id"])})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event type not found")
    return EventTypeOut(**public_doc(await attach_public_path(doc, user["slug"])))


@router.delete("/{event_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event_type(
    event_type_id: str,
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> None:
    context = await product_context(user, product_id, "manage_event_types", require_active=True)
    try:
        event_id = object_id(event_type_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event type not found") from None
    result = await get_database().event_types.delete_one({"_id": event_id, "product_id": str(context.product["_id"])})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event type not found")

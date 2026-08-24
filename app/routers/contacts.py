from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.errors import DuplicateKeyError

from app.core.database import get_database
from app.core.products import product_context
from app.core.security import get_current_user
from app.core.utils import now_utc, object_id, public_doc
from app.schemas import ContactCreate, ContactOut, ContactUpdate

router = APIRouter(prefix="/api/contacts", tags=["contacts"])


@router.get("", response_model=list[ContactOut])
async def list_contacts(
    search: str | None = Query(default=None),
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> list[ContactOut]:
    context = await product_context(user, product_id, "view_product")
    query: dict = {"product_id": str(context.product["_id"])}
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
            {"company": {"$regex": search, "$options": "i"}},
        ]
    cursor = get_database().contacts.find(query).sort("updated_at", -1)
    return [ContactOut(**public_doc(item)) async for item in cursor]


@router.post("", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
async def create_contact(
    payload: ContactCreate,
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> ContactOut:
    context = await product_context(user, product_id, "manage_contacts", require_active=True)
    timestamp = now_utc()
    contact = {
        **payload.model_dump(),
        "email": payload.email.lower(),
        "product_id": str(context.product["_id"]),
        "owner_id": str(user["_id"]),
        "source": "manual",
        "booking_count": 0,
        "last_booking_at": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = await get_database().contacts.insert_one(contact)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A contact already exists for this email")
    contact["_id"] = result.inserted_id
    return ContactOut(**public_doc(contact))


@router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: str,
    payload: ContactUpdate,
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> ContactOut:
    context = await product_context(user, product_id, "manage_contacts", require_active=True)
    try:
        oid = object_id(contact_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found") from None
    updates = payload.model_dump(exclude_unset=True)
    if updates:
        updates["updated_at"] = now_utc()
        await get_database().contacts.update_one(
            {"_id": oid, "product_id": str(context.product["_id"])},
            {"$set": updates},
        )
    contact = await get_database().contacts.find_one({"_id": oid, "product_id": str(context.product["_id"])})
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")
    return ContactOut(**public_doc(contact))


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: str,
    product_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> None:
    context = await product_context(user, product_id, "manage_contacts", require_active=True)
    try:
        oid = object_id(contact_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found") from None
    result = await get_database().contacts.delete_one({"_id": oid, "product_id": str(context.product["_id"])})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")

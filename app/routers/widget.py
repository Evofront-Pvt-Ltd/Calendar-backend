from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.core.database import get_database
from app.core.products import ensure_public_booking_token
from app.core.widget import validate_widget_origin
from app.schemas import AvailableSlotOut, ClientBookingCreatePublic, ClientBookingOut, PublicLandingProductOut, WidgetConfigOut
from app.services.product_availability import (
    build_product_slots,
    client_booking_to_out,
    create_client_booking,
    create_pending_client_booking,
    ensure_policy,
)

router = APIRouter(prefix="/api/widget", tags=["embeddable widget"])


async def product_for_widget(public_widget_id: str, request: Request, require_enabled: bool = True) -> tuple[dict, str]:
    product = await get_database().products.find_one({"public_booking_token": public_widget_id})
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Widget not found")
    origin = validate_widget_origin(product, request)
    enabled = product.get("status") == "active" and product.get("widget_enabled", True)
    if require_enabled and not enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This booking widget is not active")
    return product, origin


def product_booking_mode(product: dict) -> str:
    mode = str(product.get("booking_mode") or "").lower().strip()
    if mode in {"approval", "approval_required", "pending_approval"}:
        return "approval"
    return "instant"


async def public_product_out(product: dict) -> PublicLandingProductOut:
    policy = await ensure_policy(product)
    token = await ensure_public_booking_token(product)
    return PublicLandingProductOut(
        name=product["name"],
        description=product.get("description", ""),
        icon=product.get("icon", ""),
        color=product.get("color", "#006bff"),
        booking_token=token,
        timezone=policy["timezone"],
        support_start_time=policy["support_start_time"],
        support_end_time=policy["support_end_time"],
        appointment_duration_minutes=policy["appointment_duration_minutes"],
        booking_mode=product_booking_mode(product),
        widget_button_label=product.get("widget_button_label") or "Book Now",
        widget_action_label=product.get("widget_action_label") or "Schedule to connect team",
    )


@router.get("/{public_widget_id}/config", response_model=WidgetConfigOut)
async def widget_config(public_widget_id: str, request: Request) -> WidgetConfigOut:
    product, _origin = await product_for_widget(public_widget_id, request, require_enabled=False)
    public_product = await public_product_out(product)
    enabled = product.get("status") == "active" and product.get("widget_enabled", True)
    return WidgetConfigOut(
        workspace_name=product["name"],
        public_widget_id=public_widget_id,
        enabled=enabled,
        button_label=product.get("widget_button_label") or "Book Now",
        action_label=product.get("widget_action_label") or "Schedule to connect team",
        position=product.get("widget_position") if product.get("widget_position") in {"right", "left"} else "right",
        primary_color=product.get("color", "#006bff"),
        booking_mode=product_booking_mode(product),
        timezone=public_product.timezone,
        product=public_product,
    )


@router.get("/{public_widget_id}/products", response_model=list[PublicLandingProductOut])
async def widget_products(public_widget_id: str, request: Request) -> list[PublicLandingProductOut]:
    product, _origin = await product_for_widget(public_widget_id, request)
    return [await public_product_out(product)]


@router.get("/{public_widget_id}/availability", response_model=list[AvailableSlotOut])
async def widget_availability(
    public_widget_id: str,
    request: Request,
    availability_date: date | None = Query(default=None, alias="date"),
) -> list[AvailableSlotOut]:
    product, _origin = await product_for_widget(public_widget_id, request)
    fake_user = {
        "_id": product.get("created_by", ""),
        "organization_id": product["organization_id"],
        "name": "Embedded widget booking",
    }
    target_date = availability_date or date.today()
    slots = await build_product_slots(product, fake_user, target_date, include_internal=False)
    return [AvailableSlotOut(**slot) for slot in slots]


@router.post("/{public_widget_id}/bookings", response_model=ClientBookingOut, status_code=status.HTTP_201_CREATED)
async def widget_booking(
    public_widget_id: str,
    payload: ClientBookingCreatePublic,
    request: Request,
) -> ClientBookingOut:
    product, origin = await product_for_widget(public_widget_id, request)
    if product_booking_mode(product) == "approval":
        booking = await create_pending_client_booking(product, payload, source_domain=origin, widget_id=public_widget_id)
    else:
        booking = await create_client_booking(product, payload)
    return ClientBookingOut(**await client_booking_to_out(booking))

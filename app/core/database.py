from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING
from pymongo.errors import OperationFailure

from app.core.config import settings

client: AsyncIOMotorClient | None = None


async def connect_to_mongo() -> None:
    global client
    client = AsyncIOMotorClient(settings.mongodb_url)
    await client.admin.command("ping")
    await ensure_indexes(get_database())


async def close_mongo_connection() -> None:
    global client
    if client is not None:
        client.close()
        client = None


def get_database() -> AsyncIOMotorDatabase:
    if client is None:
        raise RuntimeError("MongoDB client has not been initialized")
    return client[settings.mongodb_db]


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.users.create_index("email", unique=True)
    await db.users.create_index("slug", unique=True)
    await db.users.create_index("organization_id")
    # Google OAuth index is parked for future reactivation.
    # await db.users.create_index(
    #     "google_sub",
    #     unique=True,
    #     partialFilterExpression={"google_sub": {"$type": "string"}},
    # )
    await db.pending_registrations.create_index("email", unique=True)
    pending_indexes = await db.pending_registrations.index_information()
    for index_name, index in pending_indexes.items():
        if index.get("key") == [("expires_at", 1)] and index.get("expireAfterSeconds") != 0:
            await db.pending_registrations.drop_index(index_name)
    await db.pending_registrations.create_index("expires_at", expireAfterSeconds=0)

    await db.refresh_tokens.create_index("token_hash", unique=True)
    await db.refresh_tokens.create_index("user_id")
    await db.refresh_tokens.create_index("expires_at", expireAfterSeconds=0)
    await db.login_attempts.create_index("email", unique=True)
    await db.login_attempts.create_index("expires_at", expireAfterSeconds=0)
    await db.password_resets.create_index("token_hash", unique=True)
    await db.password_resets.create_index("user_id")
    await db.password_resets.create_index("expires_at", expireAfterSeconds=0)

    await db.google_oauth_states.create_index("state_hash", unique=True)
    await db.google_oauth_states.create_index("expires_at", expireAfterSeconds=0)
    await db.google_oauth_states.create_index("user_id")
    await db.google_calendar_connections.create_index(
        [("user_id", ASCENDING), ("provider", ASCENDING)],
        unique=True,
    )
    await db.google_calendar_connections.create_index("provider_account_id")
    await db.google_calendar_connections.create_index("provider_email")
    await db.google_calendar_connections.create_index("connection_status")

    await db.products.create_index(
        [("organization_id", ASCENDING), ("normalized_name", ASCENDING)],
        unique=True,
    )
    await db.products.create_index("public_booking_token", unique=True, sparse=True)
    await db.products.create_index("created_by")
    await db.products.create_index("status")
    await db.product_memberships.create_index("product_id")
    await db.product_memberships.create_index("user_id")
    await db.product_memberships.create_index(
        [("product_id", ASCENDING), ("user_id", ASCENDING), ("status", ASCENDING)],
        unique=True,
        partialFilterExpression={"status": "active"},
    )
    await db.product_memberships.create_index("member_verification_token")
    await db.product_memberships.create_index("member_verification_status")

    await db.event_types.create_index(
        [("owner_id", ASCENDING), ("slug", ASCENDING)],
        unique=True,
    )
    await db.event_types.create_index("owner_id")
    await db.event_types.create_index("product_id")
    await db.bookings.create_index("owner_id")
    await db.bookings.create_index("product_id")
    await db.bookings.create_index("event_type_id")
    await db.bookings.create_index("start_utc")
    await db.bookings.create_index("google_event_id")
    await db.bookings.create_index(
        [("event_type_id", ASCENDING), ("start_utc", ASCENDING)],
        unique=True,
        partialFilterExpression={"status": "scheduled"},
    )
    await db.bookings.create_index(
        [("owner_id", ASCENDING), ("start_utc", ASCENDING)],
        unique=True,
        partialFilterExpression={"status": "scheduled"},
    )
    await db.contacts.create_index("owner_id")
    await db.contacts.create_index("product_id")
    contact_indexes = await db.contacts.index_information()
    for index_name, index in contact_indexes.items():
        if index.get("key") == [("owner_id", 1), ("email", 1)] and index.get("unique"):
            await db.contacts.drop_index(index_name)
    try:
        await db.contacts.create_index(
            [("product_id", ASCENDING), ("email", ASCENDING)],
            unique=True,
            partialFilterExpression={"product_id": {"$type": "string"}},
        )
    except OperationFailure:
        # Keep startup resilient for databases that still have legacy duplicate contacts.
        pass

    await db.meetings.create_index("product_id")
    await db.meetings.create_index("organizer_id")
    await db.meetings.create_index("start_time")
    await db.meetings.create_index("status")
    await db.meeting_invitations.create_index("meeting_id")
    await db.meeting_invitations.create_index("product_id")
    await db.meeting_invitations.create_index("recipient_user_id")
    await db.meeting_invitations.create_index("provider_message_id")
    await db.meeting_invitations.create_index("secure_token", unique=True)
    await db.meeting_invitations.create_index(
        [("meeting_id", ASCENDING), ("recipient_user_id", ASCENDING)],
        unique=True,
    )

    await db.availability_policies.create_index("product_id", unique=True)
    await db.availability_policies.create_index("organization_id")
    await db.member_availabilities.create_index("product_id")
    await db.member_availabilities.create_index("member_id")
    await db.member_availabilities.create_index([("product_id", ASCENDING), ("date", ASCENDING)])
    await db.member_availabilities.create_index(
        [
            ("product_id", ASCENDING),
            ("member_id", ASCENDING),
            ("date", ASCENDING),
            ("source", ASCENDING),
            ("start_time", ASCENDING),
        ],
        unique=True,
    )
    await db.availability_exceptions.create_index([("product_id", ASCENDING), ("exception_date", ASCENDING)])
    await db.availability_exceptions.create_index("member_id")
    await db.client_bookings.create_index("organization_id")
    await db.client_bookings.create_index("product_id")
    await db.client_bookings.create_index("assigned_member_id")
    await db.client_bookings.create_index("start_time_utc")
    await db.client_bookings.create_index("public_booking_reference", unique=True)
    await db.client_bookings.create_index("active_slot_key", unique=True, sparse=True)
    await db.client_bookings.create_index("google_event_id")
    await db.client_bookings.create_index("google_sync_status")
    await db.client_bookings.create_index(
        [
            ("product_id", ASCENDING),
            ("assigned_member_id", ASCENDING),
            ("start_time_utc", ASCENDING),
            ("status", ASCENDING),
        ],
        unique=True,
        partialFilterExpression={"status": "scheduled"},
    )
    await db.booking_assignment_history.create_index("booking_id")
    await db.booking_assignment_history.create_index("product_id")
    await db.booking_assignment_history.create_index("changed_by")
    await db.booking_notifications.create_index("product_id")
    await db.booking_notifications.create_index("booking_id")
    await db.booking_notifications.create_index("recipient_user_id")
    await db.booking_notifications.create_index("provider_message_id")
    await db.booking_notifications.create_index("idempotency_key", unique=True)
    await db.product_controllers.create_index([("product_id", ASCENDING), ("email", ASCENDING)], unique=True)
    await db.product_controllers.create_index("verification_token")
    await db.product_controllers.create_index("status")
    await db.booking_claim_alerts.create_index([("product_id", ASCENDING), ("recipient_user_id", ASCENDING), ("status", ASCENDING)])
    await db.booking_claim_alerts.create_index([("booking_id", ASCENDING), ("recipient_user_id", ASCENDING)], unique=True)
    await db.booking_claim_alerts.create_index("claim_token", unique=True)
    await db.sendgrid_events.create_index("sg_event_id", unique=True)
    await db.sendgrid_events.create_index("sg_message_id")
    await db.sendgrid_events.create_index("event")
    await db.availability_audit_logs.create_index([("product_id", ASCENDING), ("created_at", ASCENDING)])
    await db.availability_audit_logs.create_index("member_id")

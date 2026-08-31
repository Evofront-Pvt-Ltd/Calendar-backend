from datetime import timedelta
from math import ceil
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_database
from app.core.security import (
    BLOCKED_USER_STATUSES,
    create_access_token,
    get_current_user,
    get_current_user_optional,
    hash_password,
    verify_password,
)
from app.core.utils import as_utc, now_utc, object_id, public_doc, unique_slug
from app.schemas import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    RegisterResendRequest,
    RegisterStartResponse,
    RegisterVerifyRequest,
    UserPublic,
)
from app.services.auth_tokens import (
    issue_refresh_token,
    revoke_all_for_user,
    revoke_refresh_token,
    rotate_refresh_token,
)
from app.services.sendgrid import EmailProviderError, EmailVerificationMessage, sendgrid_email_provider
from app.services.scheduling import default_availability, normalize_timezone, timezone_or_400

router = APIRouter(prefix="/api/auth", tags=["auth"])
# Google OAuth imports and URLs are parked for future reactivation.
# from urllib.parse import urlencode
# from fastapi.responses import RedirectResponse
# import httpx
# from app.core.security import decode_access_token
# from app.services.google_auth import GoogleIdentity, verify_google_identity_token
# GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# MSG91 import is parked for future reactivation.
# from app.services.msg91 import EmailProviderError, EmailVerificationMessage, msg91_email_provider
OTP_EXPIRY_MINUTES = 10
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
EMAIL_ALREADY_REGISTERED_MESSAGE = "This email is already registered and verified. Please log in."
EMAIL_NOT_REGISTERED_MESSAGE = "This email is not registered. Please sign up first."
PASSWORD_INCORRECT_MESSAGE = "Password is incorrect."


def normalize_auth_email(value: str) -> str:
    return str(value).strip().lower()


def user_is_registered_and_verified(user: dict | None) -> bool:
    if user is None:
        return False
    email_verified = user.get("email_verified", True) is not False
    has_login_identity = bool(user.get("password_hash")) or user.get("auth_provider") in {
        "password",
        "google",
        "password_google",
    }
    return email_verified and has_login_identity


def raise_email_already_registered() -> None:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "success": False,
            "code": "EMAIL_ALREADY_REGISTERED",
            "message": EMAIL_ALREADY_REGISTERED_MESSAGE,
            "nextAction": "LOGIN",
        },
    )


def retry_after_seconds(value) -> int:
    return max(0, ceil((as_utc(value) - now_utc()).total_seconds()))


def registration_response(email: str, delivery_provider: str, message: str | None = None) -> RegisterStartResponse:
    message = message or (
        f"A 6-digit verification code was sent to your email. It expires in {OTP_EXPIRY_MINUTES} minutes."
        if delivery_provider == "sendgrid"
        else "Email delivery is not enabled yet. Configure SendGrid before using email verification."
    )
    return RegisterStartResponse(
        email=email,
        expires_in_minutes=OTP_EXPIRY_MINUTES,
        resend_available_in_seconds=OTP_RESEND_COOLDOWN_SECONDS,
        delivery_provider=delivery_provider,
        message=message,
    )


async def send_registration_otp(email: str, name: str, otp: str) -> str:
    if not sendgrid_email_provider.verification_enabled():
        return "console"

    try:
        await sendgrid_email_provider.send_email_verification(
            EmailVerificationMessage(
                email=email,
                name=name,
                otp=otp,
                expires_in_minutes=OTP_EXPIRY_MINUTES,
            )
        )
    except EmailProviderError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not send verification email. Check SendGrid email configuration and try again.",
        ) from None
    return "sendgrid"

    # Legacy MSG91 delivery, parked for future reactivation.
    # if not settings.msg91_email_enabled:
    #     print(f"[SIGNUP OTP] email={email} otp={otp} expires_in={OTP_EXPIRY_MINUTES}m", flush=True)
    #     return "console"
    #
    # try:
    #     await msg91_email_provider.send_email_verification(
    #         EmailVerificationMessage(
    #             email=email,
    #             name=name,
    #             otp=otp,
    #             expires_in_minutes=OTP_EXPIRY_MINUTES,
    #         )
    #     )
    # except EmailProviderError:
    #     raise HTTPException(
    #         status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    #         detail="Could not send verification email. Check MSG91 email configuration and try again.",
    #     ) from None
    # return "msg91"


def issue_access_token(user_id: str) -> tuple[str, int]:
    expires_in = settings.access_token_expire_minutes * 60
    token = create_access_token(user_id, expires_delta=timedelta(seconds=expires_in))
    return token, expires_in


async def auth_response(user: dict) -> AuthResponse:
    access_token, expires_in = issue_access_token(str(user["_id"]))
    refresh_token, _ = await issue_refresh_token(str(user["_id"]))
    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=UserPublic(**public_doc(user)),
    )


async def register_failed_login(email: str) -> None:
    window = timedelta(minutes=settings.login_attempt_window_minutes)
    await get_database().login_attempts.update_one(
        {"email": email},
        {"$inc": {"attempt_count": 1}, "$set": {"expires_at": now_utc() + window}},
        upsert=True,
    )


async def clear_failed_logins(email: str) -> None:
    await get_database().login_attempts.delete_one({"email": email})


async def guard_login_attempts(email: str) -> None:
    record = await get_database().login_attempts.find_one({"email": email})
    if record is None:
        return
    if as_utc(record["expires_at"]) <= now_utc():
        await clear_failed_logins(email)
        return
    if record.get("attempt_count", 0) >= settings.login_max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts. Please try again later.",
            headers={"Retry-After": str(retry_after_seconds(record["expires_at"]))},
        )


# Google OAuth redirect helper is parked for future reactivation.
# def frontend_redirect(path: str, params: dict[str, str]) -> RedirectResponse:
#     query = urlencode(params)
#     return RedirectResponse(f"{settings.frontend_url.rstrip('/')}{path}?{query}")


# Google OAuth account creation/linking is parked for future reactivation.
# async def get_or_create_google_user(identity: GoogleIdentity) -> dict:
#     email = identity.email
#     db = get_database()
#
#     user = await db.users.find_one({"google_sub": identity.sub})
#     if user is not None:
#         updates = {
#             "name": user.get("name") or identity.name,
#             "email_verified": True,
#             "email_verified_at": user.get("email_verified_at") or now_utc(),
#             "profile_image": identity.picture or user.get("profile_image", ""),
#             "updated_at": now_utc(),
#         }
#         if user.get("email") != email and not await db.users.find_one({"email": email}):
#             updates["email"] = email
#         await db.users.update_one(
#             {"_id": user["_id"]},
#             {"$set": updates},
#         )
#         user.update(updates)
#         return user
#
#     user = await db.users.find_one({"email": email})
#     if user is not None:
#         provider = "password_google" if user.get("password_hash") else "google"
#         updates = {
#             "auth_provider": provider,
#             "google_sub": identity.sub,
#             "email_verified": True,
#             "email_verified_at": user.get("email_verified_at") or now_utc(),
#             "profile_image": identity.picture or user.get("profile_image", ""),
#             "updated_at": now_utc(),
#         }
#         try:
#             await db.users.update_one({"_id": user["_id"]}, {"$set": updates})
#         except DuplicateKeyError:
#             linked_user = await db.users.find_one({"google_sub": identity.sub})
#             if linked_user is not None:
#                 return linked_user
#             raise
#         user.update(updates)
#         return user
#
#     name = identity.name or email.split("@", 1)[0]
#     slug = await unique_slug(db.users, name)
#     timestamp = now_utc()
#     timezone = "Asia/Kolkata"
#     user = {
#         "name": name,
#         "email": email,
#         "email_verified": True,
#         "email_verified_at": timestamp,
#         "slug": slug,
#         "timezone": timezone,
#         "availability": default_availability(timezone),
#         "password_hash": "",
#         "auth_provider": "google",
#         "google_sub": identity.sub,
#         "profile_image": identity.picture,
#         "created_at": timestamp,
#         "updated_at": timestamp,
#     }
#     try:
#         result = await db.users.insert_one(user)
#     except DuplicateKeyError:
#         existing = await db.users.find_one({"$or": [{"email": email}, {"google_sub": identity.sub}]})
#         if existing is None:
#             raise
#         return await get_or_create_google_user(identity)
#     user["_id"] = result.inserted_id
#     return user


async def create_user_from_registration(payload: RegisterRequest, password_hash: str | None = None) -> dict:
    timezone_or_400(payload.timezone)
    db = get_database()
    email = normalize_auth_email(payload.email)
    timezone = normalize_timezone(payload.timezone)
    timestamp = now_utc()
    existing = await db.users.find_one({"email": email})
    if user_is_registered_and_verified(existing):
        raise_email_already_registered()
    if existing is not None:
        slug = existing.get("slug") or await unique_slug(db.users, payload.name)
        updates = {
            "name": payload.name.strip(),
            "email": email,
            "email_verified": True,
            "email_verified_at": timestamp,
            "slug": slug,
            "timezone": timezone,
            "availability": existing.get("availability") or default_availability(timezone),
            "password_hash": password_hash or hash_password(payload.password),
            "auth_provider": "password",
            "organization_id": existing.get("organization_id") or settings.organization_id,
            "role": existing.get("role") or "member",
            "status": "active",
            "updated_at": timestamp,
        }
        await db.users.update_one({"_id": existing["_id"]}, {"$set": updates})
        existing.update(updates)
        return existing

    slug = await unique_slug(db.users, payload.name)
    user = {
        "name": payload.name.strip(),
        "email": email,
        "email_verified": True,
        "email_verified_at": timestamp,
        "slug": slug,
        "timezone": timezone,
        "availability": default_availability(timezone),
        "password_hash": password_hash or hash_password(payload.password),
        "auth_provider": "password",
        "profile_image": "",
        "organization_id": settings.organization_id,
        "role": "calendar_controller",
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = await db.users.insert_one(user)
    user["_id"] = result.inserted_id
    return user


@router.post("/register", response_model=RegisterStartResponse)
async def register(payload: RegisterRequest) -> RegisterStartResponse:
    return await register_start(payload)


@router.post("/register/start", response_model=RegisterStartResponse)
async def register_start(payload: RegisterRequest) -> RegisterStartResponse:
    timezone_or_400(payload.timezone)
    db = get_database()
    email = normalize_auth_email(payload.email)
    timezone = normalize_timezone(payload.timezone)
    existing_user = await db.users.find_one({"email": email})
    if user_is_registered_and_verified(existing_user):
        await db.pending_registrations.delete_many({"email": email})
        raise_email_already_registered()

    existing = await db.pending_registrations.find_one({"email": email})
    resend_available_at = existing.get("resend_available_at", now_utc()) if existing else now_utc()
    if existing and as_utc(resend_available_at) > now_utc():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {retry_after_seconds(resend_available_at)} seconds before requesting another code",
        )

    existing_user = await db.users.find_one({"email": email})
    if user_is_registered_and_verified(existing_user):
        await db.pending_registrations.delete_many({"email": email})
        raise_email_already_registered()

    otp = f"{secrets.randbelow(1_000_000):06d}"
    timestamp = now_utc()
    delivery_provider = await send_registration_otp(email, payload.name.strip(), otp)
    pending = {
        "name": payload.name.strip(),
        "email": email,
        "email_verified": False,
        "email_verified_at": None,
        "registration_status": "pending_verification",
        "password_hash": hash_password(payload.password),
        "timezone": timezone,
        "otp_hash": hash_password(otp),
        "expires_at": timestamp + timedelta(minutes=OTP_EXPIRY_MINUTES),
        "attempt_count": 0,
        "sent_at": timestamp,
        "resend_available_at": timestamp + timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS),
        "delivery_provider": delivery_provider,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    await db.pending_registrations.update_one({"email": email}, {"$set": pending}, upsert=True)
    return registration_response(email, delivery_provider)


@router.post("/register/resend", response_model=RegisterStartResponse)
async def register_resend(payload: RegisterResendRequest) -> RegisterStartResponse:
    db = get_database()
    email = normalize_auth_email(payload.email)
    existing_user = await db.users.find_one({"email": email})
    if user_is_registered_and_verified(existing_user):
        await db.pending_registrations.delete_many({"email": email})
        raise_email_already_registered()

    pending = await db.pending_registrations.find_one({"email": email})
    if pending is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No pending signup found for this email")
    resend_available_at = pending.get("resend_available_at", now_utc())
    if as_utc(resend_available_at) > now_utc():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {retry_after_seconds(resend_available_at)} seconds before requesting another code",
        )

    existing_user = await db.users.find_one({"email": email})
    if user_is_registered_and_verified(existing_user):
        await db.pending_registrations.delete_many({"email": email})
        raise_email_already_registered()

    otp = f"{secrets.randbelow(1_000_000):06d}"
    timestamp = now_utc()
    delivery_provider = await send_registration_otp(email, pending["name"], otp)
    await db.pending_registrations.update_one(
        {"email": email},
        {
            "$set": {
                "otp_hash": hash_password(otp),
                "expires_at": timestamp + timedelta(minutes=OTP_EXPIRY_MINUTES),
                "attempt_count": 0,
                "sent_at": timestamp,
                "resend_available_at": timestamp + timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS),
                "delivery_provider": delivery_provider,
                "updated_at": timestamp,
            }
        },
    )
    return registration_response(
        email,
        delivery_provider,
        (
            f"A new verification code was sent to your email. It expires in {OTP_EXPIRY_MINUTES} minutes."
            if delivery_provider == "sendgrid"
            else "Email delivery is not enabled yet. Configure SendGrid before resending verification codes."
        ),
    )


@router.post("/register/verify", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register_verify(payload: RegisterVerifyRequest) -> AuthResponse:
    db = get_database()
    email = normalize_auth_email(payload.email)
    existing_user = await db.users.find_one({"email": email})
    if user_is_registered_and_verified(existing_user):
        await db.pending_registrations.delete_many({"email": email})
        raise_email_already_registered()
    pending = await db.pending_registrations.find_one({"email": email})
    if pending is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No pending signup found for this email")
    if as_utc(pending["expires_at"]) < now_utc():
        await db.pending_registrations.delete_one({"email": email})
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Verification code expired. Request a new code")
    if int(pending.get("attempt_count", 0)) >= OTP_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many invalid attempts. Request a new verification code",
        )
    if not verify_password(payload.otp, pending.get("otp_hash", "")):
        attempts = int(pending.get("attempt_count", 0)) + 1
        await db.pending_registrations.update_one(
            {"email": email},
            {"$set": {"attempt_count": attempts, "updated_at": now_utc()}},
        )
        if attempts >= OTP_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many invalid attempts. Request a new verification code",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid verification code")

    try:
        existing_user = await db.users.find_one({"email": email})
        if user_is_registered_and_verified(existing_user):
            await db.pending_registrations.delete_many({"email": email})
            raise_email_already_registered()
        user = await create_user_from_registration(
            RegisterRequest(
                name=pending["name"],
                email=pending["email"],
                password="Verified123!",
                timezone=pending["timezone"],
            ),
            password_hash=pending["password_hash"],
        )
    except DuplicateKeyError:
        existing_user = await db.users.find_one({"email": email})
        if user_is_registered_and_verified(existing_user):
            await db.pending_registrations.delete_many({"email": email})
            raise_email_already_registered()
        await db.pending_registrations.delete_many({"email": email})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account already exists for this email")

    await db.pending_registrations.delete_many({"email": email})
    return await auth_response(user)


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest) -> AuthResponse:
    db = get_database()
    email = normalize_auth_email(payload.email)
    await guard_login_attempts(email)
    user = await db.users.find_one({"email": email})
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=EMAIL_NOT_REGISTERED_MESSAGE)
    if not verify_password(payload.password, user.get("password_hash", "")):
        await register_failed_login(email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=PASSWORD_INCORRECT_MESSAGE)
    if user.get("email_verified") is False:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Please verify your email before continuing")
    if user.get("status") in BLOCKED_USER_STATUSES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is no longer active")
    await clear_failed_logins(email)
    return await auth_response(user)


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(payload: RefreshRequest) -> RefreshResponse:
    rotated = await rotate_refresh_token(payload.refresh_token)
    if rotated is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
        )

    user_id, new_refresh_token, _ = rotated
    user = await get_database().users.find_one({"_id": object_id(user_id)})
    if user is None or user.get("status") in BLOCKED_USER_STATUSES:
        await revoke_all_for_user(user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
        )

    access_token, expires_in = issue_access_token(user_id)
    return RefreshResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=expires_in,
    )


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    payload: LogoutRequest,
    user: dict | None = Depends(get_current_user_optional),
) -> LogoutResponse:
    # Logout stays usable with an already-expired access token so a stale session can still be revoked.
    if payload.all_sessions:
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required to sign out of all devices",
            )
        await revoke_all_for_user(str(user["_id"]))
        return LogoutResponse(message="Signed out of all devices")
    if payload.refresh_token:
        await revoke_refresh_token(payload.refresh_token)
    return LogoutResponse()


# Google OAuth endpoints are parked for future reactivation.
# @router.get("/google/config")
# async def google_config() -> dict[str, bool | str]:
#     return {
#         "enabled": settings.google_oauth_enabled,
#         "redirect_uri": settings.google_redirect_uri,
#     }
#
#
# @router.get("/google/start")
# async def google_start() -> RedirectResponse:
#     if not settings.google_oauth_enabled:
#         return frontend_redirect(
#             "/login",
#             {"oauth_error": "Google OAuth is not configured. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."},
#         )
#
#     state = create_access_token("google-oauth", expires_delta=timedelta(minutes=10))
#     query = urlencode(
#         {
#             "client_id": settings.google_client_id,
#             "redirect_uri": settings.google_redirect_uri,
#             "response_type": "code",
#             "scope": "openid email profile",
#             "state": state,
#             "prompt": "select_account",
#         }
#     )
#     return RedirectResponse(f"{GOOGLE_AUTH_URL}?{query}")
#
#
# @router.get("/google/callback")
# async def google_callback(code: str | None = None, state: str | None = None, error: str | None = None) -> RedirectResponse:
#     if error:
#         return frontend_redirect("/login", {"oauth_error": f"Google sign-in cancelled: {error}"})
#     if not code or not state:
#         return frontend_redirect("/login", {"oauth_error": "Google did not return an authorization code."})
#
#     try:
#         payload = decode_access_token(state)
#         if payload.get("sub") != "google-oauth":
#             raise ValueError("Unexpected state subject")
#     except Exception:
#         return frontend_redirect("/login", {"oauth_error": "Google sign-in state expired. Try again."})
#
#     try:
#         async with httpx.AsyncClient(timeout=15) as client:
#             token_response = await client.post(
#                 GOOGLE_TOKEN_URL,
#                 data={
#                     "code": code,
#                     "client_id": settings.google_client_id,
#                     "client_secret": settings.google_client_secret,
#                     "redirect_uri": settings.google_redirect_uri,
#                     "grant_type": "authorization_code",
#                 },
#             )
#             token_response.raise_for_status()
#             tokens = token_response.json()
#             id_token = tokens.get("id_token")
#             if not id_token:
#                 raise ValueError("Missing Google ID token")
#
#     except (httpx.HTTPError, ValueError):
#         return frontend_redirect("/login", {"oauth_error": "Google sign-in could not be verified."})
#
#     try:
#         identity = verify_google_identity_token(id_token)
#     except HTTPException as exc:
#         return frontend_redirect("/login", {"oauth_error": str(exc.detail)})
#
#     try:
#         user = await get_or_create_google_user(identity)
#     except DuplicateKeyError:
#         return frontend_redirect("/login", {"oauth_error": "Google account could not be linked. Try again."})
#     token = create_access_token(
#         str(user["_id"]),
#         expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
#     )
#     return frontend_redirect("/oauth/google", {"token": token})


@router.get("/me", response_model=UserPublic)
async def me(user: dict = Depends(get_current_user)) -> UserPublic:
    return UserPublic(**public_doc(user))

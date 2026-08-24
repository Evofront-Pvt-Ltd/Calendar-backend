# Google OAuth helper is parked for future reactivation.
# Uncomment this file together with the Google blocks in app/routers/auth.py,
# app/core/config.py, docker-compose.yml, requirements.txt, and the frontend
# OAuth UI when you want Google sign-in back.
#
# from dataclasses import dataclass
#
# from fastapi import HTTPException, status
# from google.auth.transport import requests
# from google.oauth2 import id_token
#
# from app.core.config import settings
#
#
# @dataclass(frozen=True)
# class GoogleIdentity:
#     sub: str
#     email: str
#     email_verified: bool
#     name: str
#     picture: str
#
#
# def verify_google_identity_token(raw_id_token: str) -> GoogleIdentity:
#     try:
#         claims = id_token.verify_oauth2_token(
#             raw_id_token,
#             requests.Request(),
#             settings.google_client_id,
#         )
#     except ValueError:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Google sign-in could not be verified.",
#         ) from None
#
#     if claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Google sign-in issuer is invalid.",
#         )
#
#     if claims.get("aud") != settings.google_client_id:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Google sign-in audience is invalid.",
#         )
#
#     email = str(claims.get("email") or "").strip().lower()
#     sub = str(claims.get("sub") or "").strip()
#     email_verified = claims.get("email_verified") in (True, "true", "True", "1")
#     if not sub or not email or not email_verified:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Google account email could not be verified.",
#         )
#
#     return GoogleIdentity(
#         sub=sub,
#         email=email,
#         email_verified=True,
#         name=str(claims.get("name") or email.split("@", 1)[0]).strip(),
#         picture=str(claims.get("picture") or "").strip(),
#     )

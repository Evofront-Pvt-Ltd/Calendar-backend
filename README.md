# Calendar Backend

FastAPI service for a dynamic scheduling application backed by MongoDB.

## Run Everything With Docker

From this backend folder:

```powershell
docker compose build
docker compose up -d
```

Open:

- Frontend: `http://127.0.0.1:3000`
- Login: `http://127.0.0.1:3000/login`
- Backend API: `http://127.0.0.1:8001`
- API docs: `http://127.0.0.1:8001/docs`

## Twilio SendGrid Email OTP Setup

Signup verification now uses Twilio SendGrid when `SENDGRID_EMAIL_ENABLED=true`.

1. In SendGrid, finish sender authentication for the domain you will send from.
2. Create a SendGrid API key with Mail Send access.
3. Copy `.env.example` to `.env`.
4. Fill the SendGrid values:

```text
SENDGRID_EMAIL_ENABLED=true
SENDGRID_API_KEY=<your-sendgrid-api-key>
SENDGRID_MAIL_SEND_URL=https://api.sendgrid.com/v3/mail/send
SENDGRID_FROM_EMAIL=no-reply@your-domain.com
SENDGRID_FROM_NAME=Calendar Booking
SENDGRID_REPLY_TO_EMAIL=support@your-domain.com
SENDGRID_TEMPLATE_ID=
SENDGRID_SANDBOX_MODE=false
```

`SENDGRID_TEMPLATE_ID` is optional. If it is blank, the backend sends a built-in plain-text and HTML OTP email. If you set a dynamic template ID, include variables such as `{{otp}}`, `{{name}}`, `{{email}}`, `{{expires_in_minutes}}`, `{{app_name}}`, or `{{verification_code}}`.

Then restart:

```powershell
docker compose up -d --build
```

## Product-Scoped Calendar Setup

New and existing accounts are automatically attached to a default product the first time product-scoped APIs are used. Legacy `event_types`, `bookings`, and `contacts` without `product_id` are assigned to that default product during the same pass.

Required environment values:

```text
APPLICATION_BASE_URL=http://127.0.0.1:3000
ORGANIZATION_ID=default
ORGANIZATION_EMAIL_DOMAIN=evofront.com
EMAIL_ENABLED=false
EMAIL_PROVIDER=sendgrid
```

`ORGANIZATION_EMAIL_DOMAIN` is enforced by the backend when adding product team members. `EMAIL_ENABLED=false` still creates meetings and invitation records, with delivery status `PENDING_EMAIL_INTEGRATION` and copyable invitation links.

## Authentication

Sessions use a short-lived HS256 access token plus a rotating opaque refresh token.

- `POST /api/auth/login` and `POST /api/auth/register/verify` return `access_token`, `refresh_token`, and `expires_in`.
- Send the access token as `Authorization: Bearer <access-token>`; it expires after `ACCESS_TOKEN_EXPIRE_MINUTES` (default 30).
- `POST /api/auth/refresh` exchanges a refresh token for a new pair. The old refresh token is revoked on use, and presenting it again revokes every session for that user as a theft signal.
- `POST /api/auth/logout` revokes the supplied refresh token; pass `{"all_sessions": true}` with a valid access token to sign out everywhere.
- Refresh tokens live in the `refresh_tokens` collection as SHA-256 hashes and expire after `REFRESH_TOKEN_EXPIRE_DAYS` (default 30).
- Login is throttled per email: `LOGIN_MAX_ATTEMPTS` failures within `LOGIN_ATTEMPT_WINDOW_MINUTES` return 429 until the window lapses.
- Outside development, the app refuses to start if `JWT_SECRET` is a known placeholder or shorter than 32 characters.

Main authenticated endpoints:

- `GET /api/products`
- `POST /api/products`
- `GET /api/products/{product_id}`
- `PATCH /api/products/{product_id}`
- `GET /api/products/{product_id}/members`
- `POST /api/products/{product_id}/members`
- `PATCH /api/products/{product_id}/members/{membership_id}`
- `DELETE /api/products/{product_id}/members/{membership_id}`
- `GET /api/products/{product_id}/meetings`
- `POST /api/products/{product_id}/meetings`
- `GET /api/public/invitations/{token}`

Existing dashboard endpoints accept `product_id` as a query parameter: `/api/dashboard/stats`, `/api/availability`, `/api/event-types`, `/api/bookings`, and `/api/contacts`.

Legacy Google OAuth code is commented in `app/routers/auth.py`, `app/services/google_auth.py`, `app/core/config.py`, `docker-compose.yml`, and `requirements.txt` for future reactivation.

## Embeddable Workspace Widget

Each product can act as a v1 workspace for an external website. Configure these values from Product Settings:

- Approved website domains, for example `https://www.websitex.com`
- Controller emails, which receive new booking requests for that workspace once verified
- Support email
- Booking mode: `instant` or `approval`
- Widget enabled/disabled
- Widget button/action labels and side position

## Verified Work Emails and Shift Claims

Team members and controller mailboxes both use a 7-day "Verify this mail" link, and
verification is independent of dashboard login:

- Adding a team member sends a verification email to their work address
- Only verified members join the equal-shift rotation and can be assigned bookings
- A verified member with a login gets a dashboard claim alert plus email; a member
  without a login gets email only and accepts from `/booking-claim/{token}`
- The first accept wins; remaining alerts close
- If nobody is on shift for the requested slot, alerts fall back to controllers
- When Google Calendar is enabled and the accepting member has no connected calendar,
  the approver's calendar is used, then the workspace owner's

Public verification endpoints:

- `GET /api/public/member-verify/{token}`
- `GET /api/public/controller-verify/{token}`

Install the widget on the approved website with the product's public widget ID:

```html
<script
  src="https://your-calendar-platform.example.com/widget.js"
  data-workspace-id="public_widget_id"
  data-position="right"
  async>
</script>
```

The snippet contains no secrets. The backend maps `data-workspace-id` to one active product/workspace and validates the host origin against that product's approved domains before returning config, availability, or creating a booking. Localhost and `127.0.0.1` are allowed as development origins when no approved domains are configured.

Widget endpoints:

- `GET /api/widget/{public_widget_id}/config`
- `GET /api/widget/{public_widget_id}/products`
- `GET /api/widget/{public_widget_id}/availability?date=YYYY-MM-DD`
- `POST /api/widget/{public_widget_id}/bookings`

Controller endpoints for workspace bookings:

- `PATCH /api/availability/bookings/{booking_id}/assignment?product_id={product_id}`
- `POST /api/availability/bookings/{booking_id}/approve?product_id={product_id}`
- `POST /api/availability/bookings/{booking_id}/reject?product_id={product_id}`

## Signup Verification

By default, signup verifies new users with a 6-digit OTP printed in the backend console. This keeps local signup working before SendGrid keys are configured.

With Docker Compose, read the OTP with:

```powershell
docker compose logs backend
```

Look for:

```text
[SIGNUP OTP] email=user@example.com otp=123456 expires_in=10m
```

The backend never returns the OTP through the API. In console mode it prints the OTP to backend logs. In SendGrid mode it sends the OTP through Twilio SendGrid instead.

Legacy MSG91 code is commented in `app/routers/auth.py`, `app/services/msg91.py`, `app/core/config.py`, `docker-compose.yml`, and `.env.example` for future reactivation.

## Local Run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API is available at `http://localhost:8000`, with Swagger docs at `http://localhost:8000/docs`.

## Core Features

- Email/password registration with console OTP locally, and SendGrid email OTP when enabled
- Legacy Google OAuth and MSG91 email delivery parked in comments for future use
- Product selector support with server-enforced product membership
- Product-specific teams, availability, event links, bookings, contacts, meetings, and invitations
- Provider-independent meeting invitation delivery layer with SendGrid support
- Availability rules with weekly windows, buffer time, slot interval, and minimum notice
- Event type CRUD with public scheduling links
- Public slot lookup and booking creation
- Owner booking dashboard and cancellation
- Contact CRUD, plus automatic contact creation from confirmed bookings

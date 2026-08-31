#!/usr/bin/env bash
set -euo pipefail

: "${KUBE_CONFIG_DATA:?KUBECONFIG secret is required}"

generate_jwt_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
    return
  fi
  head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

# shellcheck source=kubeconfig_env.sh
source "$(dirname "$0")/kubeconfig_env.sh"
setup_kubeconfig

kubectl create namespace calendar-backend --dry-run=client -o yaml | kubectl apply -f -

existing_secret_value() {
  kubectl -n calendar-backend get secret calendar-backend-secrets \
    -o "jsonpath={.data.$1}" 2>/dev/null | base64 -d 2>/dev/null || true
}

if kubectl -n calendar-backend get secret calendar-backend-secrets >/dev/null 2>&1; then
  # Keep the stored value for any input the workflow did not supply, so setting a
  # single new secret in GitHub propagates without requiring all the others.
  JWT_SECRET_VALUE="${CALENDAR_JWT_SECRET:-$(existing_secret_value JWT_SECRET)}"
  SENDGRID_API_KEY_VALUE="${CALENDAR_SENDGRID_API_KEY:-$(existing_secret_value SENDGRID_API_KEY)}"
  SENDGRID_WEBHOOK_KEY_VALUE="${CALENDAR_SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY:-$(existing_secret_value SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY)}"
  GOOGLE_CLIENT_SECRET_VALUE="${CALENDAR_GOOGLE_CLIENT_SECRET:-$(existing_secret_value GOOGLE_CLIENT_SECRET)}"
  GOOGLE_TOKEN_KEY_VALUE="${CALENDAR_GOOGLE_TOKEN_ENCRYPTION_KEY:-$(existing_secret_value GOOGLE_TOKEN_ENCRYPTION_KEY)}"

  kubectl -n calendar-backend create secret generic calendar-backend-secrets \
    --from-literal=JWT_SECRET="${JWT_SECRET_VALUE}" \
    --from-literal=SENDGRID_API_KEY="${SENDGRID_API_KEY_VALUE}" \
    --from-literal=SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY="${SENDGRID_WEBHOOK_KEY_VALUE}" \
    --from-literal=GOOGLE_CLIENT_SECRET="${GOOGLE_CLIENT_SECRET_VALUE}" \
    --from-literal=GOOGLE_TOKEN_ENCRYPTION_KEY="${GOOGLE_TOKEN_KEY_VALUE}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "Updated calendar-backend-secrets in namespace calendar-backend"

  if [ -z "${SENDGRID_API_KEY_VALUE}" ]; then
    echo "::warning::SENDGRID_API_KEY is empty. Signup verification emails will fail with HTTP 503 because EMAIL_ENABLED is true. Set CALENDAR_SENDGRID_API_KEY in GitHub Actions secrets."
  fi
  exit 0
fi

if [ -z "${CALENDAR_JWT_SECRET:-}" ]; then
  CALENDAR_JWT_SECRET="$(generate_jwt_secret)"
  echo "::warning::CALENDAR_JWT_SECRET is not set in GitHub. Created a bootstrap JWT secret in the cluster for staging. Add CALENDAR_JWT_SECRET in GitHub Actions secrets and re-run deploy to pin a stable value."
fi

kubectl -n calendar-backend create secret generic calendar-backend-secrets \
  --from-literal=JWT_SECRET="${CALENDAR_JWT_SECRET}" \
  --from-literal=SENDGRID_API_KEY="${CALENDAR_SENDGRID_API_KEY:-}" \
  --from-literal=SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY="${CALENDAR_SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY:-}" \
  --from-literal=GOOGLE_CLIENT_SECRET="${CALENDAR_GOOGLE_CLIENT_SECRET:-}" \
  --from-literal=GOOGLE_TOKEN_ENCRYPTION_KEY="${CALENDAR_GOOGLE_TOKEN_ENCRYPTION_KEY:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Ensured calendar-backend-secrets in namespace calendar-backend"

if [ -z "${CALENDAR_SENDGRID_API_KEY:-}" ]; then
  echo "::warning::SENDGRID_API_KEY is empty. Signup verification emails will fail with HTTP 503 because EMAIL_ENABLED is true. Set CALENDAR_SENDGRID_API_KEY in GitHub Actions secrets."
fi

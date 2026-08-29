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

if kubectl -n calendar-backend get secret calendar-backend-secrets >/dev/null 2>&1; then
  if [ -n "${CALENDAR_JWT_SECRET:-}" ]; then
    kubectl -n calendar-backend create secret generic calendar-backend-secrets \
      --from-literal=JWT_SECRET="${CALENDAR_JWT_SECRET}" \
      --from-literal=SENDGRID_API_KEY="${CALENDAR_SENDGRID_API_KEY:-}" \
      --from-literal=SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY="${CALENDAR_SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY:-}" \
      --from-literal=GOOGLE_CLIENT_SECRET="${CALENDAR_GOOGLE_CLIENT_SECRET:-}" \
      --from-literal=GOOGLE_TOKEN_ENCRYPTION_KEY="${CALENDAR_GOOGLE_TOKEN_ENCRYPTION_KEY:-}" \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "Updated calendar-backend-secrets in namespace calendar-backend"
  else
    echo "calendar-backend-secrets already exists; leaving unchanged (set CALENDAR_JWT_SECRET to update)"
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

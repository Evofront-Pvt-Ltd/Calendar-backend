#!/usr/bin/env bash
set -euo pipefail

: "${KUBE_CONFIG_DATA:?KUBECONFIG secret is required}"

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
  fi
  exit 0
fi

if [ -z "${CALENDAR_JWT_SECRET:-}" ]; then
  echo "::error::Secret calendar-backend-secrets not found in namespace calendar-backend. Add CALENDAR_JWT_SECRET in GitHub Actions secrets or run DEPLOY.md step 3."
  exit 1
fi

kubectl -n calendar-backend create secret generic calendar-backend-secrets \
  --from-literal=JWT_SECRET="${CALENDAR_JWT_SECRET}" \
  --from-literal=SENDGRID_API_KEY="${CALENDAR_SENDGRID_API_KEY:-}" \
  --from-literal=SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY="${CALENDAR_SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY:-}" \
  --from-literal=GOOGLE_CLIENT_SECRET="${CALENDAR_GOOGLE_CLIENT_SECRET:-}" \
  --from-literal=GOOGLE_TOKEN_ENCRYPTION_KEY="${CALENDAR_GOOGLE_TOKEN_ENCRYPTION_KEY:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

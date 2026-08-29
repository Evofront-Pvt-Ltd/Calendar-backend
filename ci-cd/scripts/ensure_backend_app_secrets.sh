#!/usr/bin/env bash
set -euo pipefail

if [ -z "${KUBE_CONFIG_DATA:-}" ]; then
  echo "KUBECONFIG secret is not set; skipping backend application secret bootstrap"
  exit 0
fi

if [ -z "${CALENDAR_JWT_SECRET:-}" ]; then
  echo "CALENDAR_JWT_SECRET is not set; skipping backend application secret bootstrap"
  exit 0
fi

# shellcheck source=kubeconfig_env.sh
source "$(dirname "$0")/kubeconfig_env.sh"
setup_kubeconfig

kubectl -n calendar-backend create secret generic calendar-backend-secrets \
  --from-literal=JWT_SECRET="${CALENDAR_JWT_SECRET}" \
  --from-literal=SENDGRID_API_KEY="${CALENDAR_SENDGRID_API_KEY:-}" \
  --from-literal=SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY="${CALENDAR_SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY:-}" \
  --from-literal=GOOGLE_CLIENT_SECRET="${CALENDAR_GOOGLE_CLIENT_SECRET:-}" \
  --from-literal=GOOGLE_TOKEN_ENCRYPTION_KEY="${CALENDAR_GOOGLE_TOKEN_ENCRYPTION_KEY:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Ensured calendar-backend-secrets in namespace calendar-backend"

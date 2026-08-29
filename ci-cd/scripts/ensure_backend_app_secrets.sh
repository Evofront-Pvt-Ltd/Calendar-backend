#!/usr/bin/env bash
set -euo pipefail

if [ -z "${KUBE_CONFIG_DATA:-}" ]; then
  echo "KUBECONFIG secret is not set; skipping backend application secret bootstrap"
  exit 0
fi

if [ -z "${CALENDAR_JWT_SECRET:-}" ]; then
  echo "::warning::CALENDAR_JWT_SECRET is not set in GitHub repository secrets. Add it under Settings → Secrets → Actions, then re-run deploy."
  echo "Skipping backend application secret bootstrap"
  exit 0
fi

mkdir -p "$HOME/.kube"
if printf '%s' "${KUBE_CONFIG_DATA}" | grep -qE '^apiVersion:'; then
  printf '%s' "${KUBE_CONFIG_DATA}" > "$HOME/.kube/config"
else
  printf '%s' "${KUBE_CONFIG_DATA}" | base64 -d > "$HOME/.kube/config"
fi
chmod 600 "$HOME/.kube/config"
export KUBECONFIG="$HOME/.kube/config"

kubectl -n calendar-backend create secret generic calendar-backend-secrets \
  --from-literal=JWT_SECRET="${CALENDAR_JWT_SECRET}" \
  --from-literal=SENDGRID_API_KEY="${CALENDAR_SENDGRID_API_KEY:-}" \
  --from-literal=SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY="${CALENDAR_SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY:-}" \
  --from-literal=GOOGLE_CLIENT_SECRET="${CALENDAR_GOOGLE_CLIENT_SECRET:-}" \
  --from-literal=GOOGLE_TOKEN_ENCRYPTION_KEY="${CALENDAR_GOOGLE_TOKEN_ENCRYPTION_KEY:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Ensured calendar-backend-secrets in namespace calendar-backend"

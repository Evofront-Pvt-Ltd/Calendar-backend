#!/usr/bin/env bash
set -euo pipefail

: "${DOCKERHUB_USERNAME:?DOCKERHUB_USERNAME is required}"
: "${DOCKERHUB_PASSWORD:?DOCKERHUB_PASSWORD is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${KUBE_CONFIG_DATA:?KUBECONFIG secret is required}"
: "${API_HEALTH_URL:?API_HEALTH_URL is required}"
: "${APP_HEALTH_URL:?APP_HEALTH_URL is required}"
: "${IMAGE_NAME:=calendar-backend}"

REMOTE_TAG="${DOCKERHUB_USERNAME}/${IMAGE_NAME}:${GITHUB_SHA}"

docker build -t "${IMAGE_NAME}:${GITHUB_SHA}" .
docker tag "${IMAGE_NAME}:${GITHUB_SHA}" "${REMOTE_TAG}"
docker push "${REMOTE_TAG}"
docker manifest inspect "${REMOTE_TAG}" >/dev/null

bash ci-cd/scripts/ensure_dockerhub_pull_secret.sh calendar-backend
bash ci-cd/scripts/ensure_backend_app_secrets.sh

# shellcheck source=kubeconfig_env.sh
source ci-cd/scripts/kubeconfig_env.sh
setup_kubeconfig

kubectl -n calendar-backend set image deployment/calendar-backend \
  calendar-backend="${REMOTE_TAG}"
kubectl -n calendar-backend rollout status deployment/calendar-backend --timeout=300s

bash ci-cd/scripts/verify_https_health.sh

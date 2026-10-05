#!/usr/bin/env bash
set -euo pipefail

: "${DOCKERHUB_USERNAME:?DOCKERHUB_USERNAME is required}"
: "${DOCKERHUB_PASSWORD:?DOCKERHUB_PASSWORD is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${KUBE_CONFIG_DATA:=}"
: "${API_HEALTH_URL:?API_HEALTH_URL is required}"
: "${APP_HEALTH_URL:?APP_HEALTH_URL is required}"
: "${IMAGE_NAME:=calendar-backend}"

REMOTE_TAG="${DOCKERHUB_USERNAME}/${IMAGE_NAME}:${GITHUB_SHA}"

docker build -t "${IMAGE_NAME}:${GITHUB_SHA}" .
docker tag "${IMAGE_NAME}:${GITHUB_SHA}" "${REMOTE_TAG}"
docker push "${REMOTE_TAG}"
docker manifest inspect "${REMOTE_TAG}" >/dev/null

if [[ -n "${KUBE_CONFIG_DATA:-}" ]]; then
  bash ci-cd/scripts/ensure_dockerhub_pull_secret.sh calendar-backend
  bash ci-cd/scripts/ensure_backend_app_secrets.sh
  bash ci-cd/scripts/relieve_disk_pressure.sh || true
fi

bash ci-cd/scripts/commit_sha_manifest.sh

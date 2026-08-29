#!/usr/bin/env bash
set -euo pipefail

: "${DOCKERHUB_USERNAME:?DOCKERHUB_USERNAME is required}"
: "${DOCKERHUB_PASSWORD:?DOCKERHUB_PASSWORD is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_ACTOR:?GITHUB_ACTOR is required}"
: "${GITHUB_ACTOR_ID:?GITHUB_ACTOR_ID is required}"
: "${IMAGE_NAME:=calendar-backend}"

REMOTE_TAG="${DOCKERHUB_USERNAME}/${IMAGE_NAME}:${GITHUB_SHA}"

docker build -t "${IMAGE_NAME}:${GITHUB_SHA}" .
docker tag "${IMAGE_NAME}:${GITHUB_SHA}" "${REMOTE_TAG}"
docker push "${REMOTE_TAG}"
docker manifest inspect "${REMOTE_TAG}" >/dev/null

bash ci-cd/scripts/ensure_dockerhub_pull_secret.sh calendar-backend
bash ci-cd/scripts/ensure_backend_app_secrets.sh

python3 ci-cd/scripts/set_deployment_image.py "${DOCKERHUB_USERNAME}/${IMAGE_NAME}" "${GITHUB_SHA}"

git config user.name "${GITHUB_ACTOR}"
git config user.email "${GITHUB_ACTOR_ID}+${GITHUB_ACTOR}@users.noreply.github.com"
git add k8s/overlays/staging/kustomization.yaml
git diff --staged --quiet && exit 0
git commit -m "k8s: pin calendar-backend image to ${GITHUB_SHA}"
git push

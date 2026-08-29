#!/usr/bin/env bash
set -euo pipefail

: "${DOCKERHUB_USERNAME:?DOCKERHUB_USERNAME is required}"
: "${DOCKERHUB_PASSWORD:?DOCKERHUB_PASSWORD is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_ACTOR:?GITHUB_ACTOR is required}"
: "${GITHUB_ACTOR_ID:?GITHUB_ACTOR_ID is required}"
: "${IMAGE_NAME:=calendar-backend}"

docker build -t "${IMAGE_NAME}:${GITHUB_SHA}" .
docker tag "${IMAGE_NAME}:${GITHUB_SHA}" "${DOCKERHUB_USERNAME}/${IMAGE_NAME}:${GITHUB_SHA}"
docker push "${DOCKERHUB_USERNAME}/${IMAGE_NAME}:${GITHUB_SHA}"
python3 ci-cd/scripts/set_deployment_image.py "${DOCKERHUB_USERNAME}/${IMAGE_NAME}" "${GITHUB_SHA}"

git config user.name "${GITHUB_ACTOR}"
git config user.email "${GITHUB_ACTOR_ID}+${GITHUB_ACTOR}@users.noreply.github.com"
git add k8s/overlays/staging/kustomization.yaml
git diff --staged --quiet && exit 0
git commit -m "k8s: pin calendar-backend image to ${GITHUB_SHA}"
git push

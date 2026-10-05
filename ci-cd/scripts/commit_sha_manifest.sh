#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${DOCKERHUB_USERNAME:?DOCKERHUB_USERNAME is required}"
: "${IMAGE_NAME:?IMAGE_NAME is required}"

KUSTOMIZE_FILE="k8s/overlays/staging/kustomization.yaml"
sed -i "s|newName: .*|newName: ${DOCKERHUB_USERNAME}/${IMAGE_NAME}|" "${KUSTOMIZE_FILE}"
sed -i "s/newTag: .*/newTag: ${GITHUB_SHA}/" "${KUSTOMIZE_FILE}"

git add "${KUSTOMIZE_FILE}"
if git diff --cached --quiet; then
  echo "Kustomize image already ${DOCKERHUB_USERNAME}/${IMAGE_NAME}:${GITHUB_SHA}"
  exit 0
fi

MSG="$(git log -1 --format=%B)"
git -c user.name="bhavya25-ef" -c user.email="bhavya.k@evofront.com" commit -m "${MSG}"
git push origin "HEAD:${GITHUB_REF_NAME:-develop}"
echo "Pushed SHA tag ${GITHUB_SHA} with the same commit message for Argo CD"

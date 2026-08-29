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
bash ci-cd/scripts/ensure_mongodb_pvc.sh
bash ci-cd/scripts/ensure_backend_app_secrets.sh

# shellcheck source=kubeconfig_env.sh
source ci-cd/scripts/kubeconfig_env.sh
setup_kubeconfig

if kubectl -n calendar-backend get deployment mongodb >/dev/null 2>&1; then
  kubectl -n calendar-backend rollout status deployment/mongodb --timeout=180s
fi

KUSTOMIZE_FILE="k8s/overlays/staging/kustomization.yaml"
sed -i "s|newName: .*|newName: ${DOCKERHUB_USERNAME}/${IMAGE_NAME}|" "${KUSTOMIZE_FILE}"
sed -i "s/newTag: .*/newTag: ${GITHUB_SHA}/" "${KUSTOMIZE_FILE}"
kubectl apply -k k8s/overlays/staging

if ! kubectl -n calendar-backend rollout status deployment/calendar-backend --timeout=420s; then
  echo "::group::Backend rollout diagnostics"
  kubectl -n calendar-backend get pods -l app=calendar-backend -o wide || true
  kubectl -n calendar-backend describe pods -l app=calendar-backend || true
  kubectl -n calendar-backend logs -l app=calendar-backend --tail=120 --all-containers=true || true
  echo "::endgroup::"
  exit 1
fi

bash ci-cd/scripts/verify_https_health.sh

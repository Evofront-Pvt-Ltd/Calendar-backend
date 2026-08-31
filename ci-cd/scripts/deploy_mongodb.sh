#!/usr/bin/env bash
set -euo pipefail

: "${KUBE_CONFIG_DATA:?KUBECONFIG secret is required}"

source ci-cd/scripts/kubeconfig_env.sh
setup_kubeconfig

MODE="$(bash ci-cd/scripts/detect_mongodb_mode.sh)"
if [ "${MODE}" = "legacy" ]; then
  NS="calendar-backend"
  OVERLAY="k8s/overlays/staging/calendar-mongodb-legacy"
else
  bash ci-cd/scripts/ensure_mongodb_pvc.sh
  NS="calendar-mongodb"
  OVERLAY="k8s/overlays/staging/calendar-mongodb"
fi

kubectl apply -f k8s/bootstrap/namespaces.yaml
kubectl apply -k "${OVERLAY}"

if ! kubectl -n "${NS}" rollout status deployment/mongodb --timeout=420s; then
  echo "::group::MongoDB rollout diagnostics"
  kubectl -n "${NS}" get pods -l app=mongodb -o wide || true
  kubectl -n "${NS}" describe pods -l app=mongodb || true
  kubectl -n "${NS}" logs -l app=mongodb --tail=120 --all-containers=true || true
  echo "::endgroup::"
  exit 1
fi

bash ci-cd/scripts/ensure_argocd_application.sh
echo "MongoDB mode: ${MODE}"
echo "MongoDB namespace: ${NS}"

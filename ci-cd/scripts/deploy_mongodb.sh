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
kubectl -n "${NS}" rollout status deployment/mongodb --timeout=180s
echo "MongoDB mode: ${MODE}"
echo "MongoDB namespace: ${NS}"

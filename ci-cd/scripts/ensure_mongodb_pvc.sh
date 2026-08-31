#!/usr/bin/env bash
set -euo pipefail

if [ -z "${KUBE_CONFIG_DATA:-}" ]; then
  echo "KUBECONFIG secret is not set; skipping MongoDB PVC bootstrap"
  exit 0
fi

source "$(dirname "$0")/kubeconfig_env.sh"
setup_kubeconfig

MODE="$(bash "$(dirname "$0")/detect_mongodb_mode.sh")"
if [ "${MODE}" = "legacy" ]; then
  NS="calendar-backend"
  PVC_FILE="k8s/bootstrap/mongodb-pvc.yaml"
else
  NS="calendar-mongodb"
  PVC_FILE="k8s/bootstrap/mongodb-pvc-isolated.yaml"
fi

kubectl apply -f k8s/bootstrap/namespaces.yaml

detach_pvc_from_argocd() {
  kubectl -n "${NS}" annotate pvc calendar-mongodb-data \
    argocd.argoproj.io/sync-options=Delete=false --overwrite
  kubectl -n "${NS}" label pvc calendar-mongodb-data \
    argocd.argoproj.io/instance- 2>/dev/null || true
  kubectl -n "${NS}" annotate pvc calendar-mongodb-data \
    argocd.argoproj.io/tracking-id- 2>/dev/null || true
}

if kubectl -n "${NS}" get pvc calendar-mongodb-data >/dev/null 2>&1; then
  detach_pvc_from_argocd
  echo "MongoDB PVC calendar-mongodb-data detached from Argo CD tracking in ${NS}"
  exit 0
fi

kubectl apply -f "${PVC_FILE}"
detach_pvc_from_argocd
echo "Ensured MongoDB PVC calendar-mongodb-data in namespace ${NS}"

#!/usr/bin/env bash
set -euo pipefail

if [ -z "${KUBE_CONFIG_DATA:-}" ]; then
  echo "KUBECONFIG secret is not set; skipping MongoDB PVC bootstrap"
  exit 0
fi

# shellcheck source=kubeconfig_env.sh
source "$(dirname "$0")/kubeconfig_env.sh"
setup_kubeconfig

kubectl create namespace calendar-backend --dry-run=client -o yaml | kubectl apply -f -

detach_pvc_from_argocd() {
  kubectl -n calendar-backend annotate pvc calendar-mongodb-data \
    argocd.argoproj.io/sync-options=Delete=false --overwrite
  kubectl -n calendar-backend label pvc calendar-mongodb-data \
    argocd.argoproj.io/instance- 2>/dev/null || true
  kubectl -n calendar-backend annotate pvc calendar-mongodb-data \
    argocd.argoproj.io/tracking-id- 2>/dev/null || true
}

if kubectl -n calendar-backend get pvc calendar-mongodb-data >/dev/null 2>&1; then
  detach_pvc_from_argocd
  echo "MongoDB PVC calendar-mongodb-data detached from Argo CD tracking"
  exit 0
fi

kubectl apply -f k8s/bootstrap/mongodb-pvc.yaml
detach_pvc_from_argocd
echo "Ensured MongoDB PVC calendar-mongodb-data in namespace calendar-backend"

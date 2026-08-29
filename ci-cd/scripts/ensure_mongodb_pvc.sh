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

annotate_pvc() {
  kubectl -n calendar-backend annotate pvc calendar-mongodb-data \
    argocd.argoproj.io/sync-options=Delete=false --overwrite
}

if kubectl -n calendar-backend get pvc calendar-mongodb-data >/dev/null 2>&1; then
  annotate_pvc
  echo "MongoDB PVC calendar-mongodb-data already exists; annotated for Argo CD orphan"
  exit 0
fi

kubectl apply -f k8s/bootstrap/mongodb-pvc.yaml
annotate_pvc
echo "Ensured MongoDB PVC calendar-mongodb-data in namespace calendar-backend"

#!/usr/bin/env bash
set -euo pipefail

if [ -z "${KUBE_CONFIG_DATA:-}" ]; then
  exit 0
fi

source "$(dirname "$0")/kubeconfig_env.sh"
setup_kubeconfig
kubectl apply -f k8s/argocd/application.yaml

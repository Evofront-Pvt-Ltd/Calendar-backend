#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <namespace> [namespace...]" >&2
  exit 1
fi

if [ -z "${KUBE_CONFIG_DATA:-}" ]; then
  echo "KUBECONFIG secret is not set; skipping Docker Hub pull-secret bootstrap"
  exit 0
fi

if [ -z "${DOCKERHUB_USERNAME:-}" ] || [ -z "${DOCKERHUB_PASSWORD:-}" ]; then
  echo "::error::DOCKERHUB_USERNAME and DOCKERHUB_PASSWORD are required for pull-secret bootstrap" >&2
  exit 1
fi

# shellcheck source=kubeconfig_env.sh
source "$(dirname "$0")/kubeconfig_env.sh"
setup_kubeconfig

for ns in "$@"; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "$ns" create secret docker-registry dockerhub-pull \
    --docker-server=https://index.docker.io/v1/ \
    --docker-username="${DOCKERHUB_USERNAME}" \
    --docker-password="${DOCKERHUB_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "Ensured dockerhub-pull secret in namespace ${ns}"
done

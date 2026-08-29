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

mkdir -p "$HOME/.kube"
if printf '%s' "${KUBE_CONFIG_DATA}" | grep -qE '^apiVersion:'; then
  printf '%s' "${KUBE_CONFIG_DATA}" > "$HOME/.kube/config"
else
  printf '%s' "${KUBE_CONFIG_DATA}" | base64 -d > "$HOME/.kube/config"
  if ! grep -qE '^apiVersion:' "$HOME/.kube/config"; then
    echo "::error::KUBECONFIG secret must be raw kubeconfig YAML or base64 kubeconfig YAML" >&2
    exit 1
  fi
fi
chmod 600 "$HOME/.kube/config"
export KUBECONFIG="$HOME/.kube/config"

kubectl config current-context >/dev/null
kubectl cluster-info >/dev/null

for ns in "$@"; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "$ns" create secret docker-registry dockerhub-pull \
    --docker-server=https://index.docker.io/v1/ \
    --docker-username="${DOCKERHUB_USERNAME}" \
    --docker-password="${DOCKERHUB_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "Ensured dockerhub-pull secret in namespace ${ns}"
done

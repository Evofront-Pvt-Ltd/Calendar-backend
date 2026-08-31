#!/usr/bin/env bash
set -euo pipefail

if [ -z "${KUBE_CONFIG_DATA:-}" ]; then
  echo "legacy"
  exit 0
fi

source "$(dirname "$0")/kubeconfig_env.sh"
setup_kubeconfig

if kubectl -n calendar-backend get pvc calendar-mongodb-data >/dev/null 2>&1; then
  echo "legacy"
  exit 0
fi

echo "isolated"

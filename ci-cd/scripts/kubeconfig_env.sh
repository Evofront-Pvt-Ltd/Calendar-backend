#!/usr/bin/env bash
setup_kubeconfig() {
  if [ -z "${KUBE_CONFIG_DATA:-}" ]; then
    return 1
  fi
  mkdir -p "$HOME/.kube"
  if printf '%s' "${KUBE_CONFIG_DATA}" | grep -qE '^apiVersion:'; then
    printf '%s' "${KUBE_CONFIG_DATA}" > "$HOME/.kube/config"
  else
    printf '%s' "${KUBE_CONFIG_DATA}" | base64 -d > "$HOME/.kube/config"
  fi
  chmod 600 "$HOME/.kube/config"
  export KUBECONFIG="$HOME/.kube/config"
  kubectl config current-context >/dev/null
  kubectl cluster-info >/dev/null
}

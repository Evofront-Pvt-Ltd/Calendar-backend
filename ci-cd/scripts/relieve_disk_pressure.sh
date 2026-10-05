#!/usr/bin/env bash
set -euo pipefail

if [ -z "${KUBE_CONFIG_DATA:-}" ]; then
  echo "KUBECONFIG not set; skip disk relief"
  exit 0
fi

if ! command -v kubectl >/dev/null 2>&1; then
  mkdir -p "${HOME}/bin"
  curl -fsSL -o "${HOME}/bin/kubectl" "https://dl.k8s.io/release/v1.31.13/bin/linux/amd64/kubectl"
  chmod +x "${HOME}/bin/kubectl"
  export PATH="${HOME}/bin:${PATH}"
fi

# shellcheck source=kubeconfig_env.sh
source "$(dirname "$0")/kubeconfig_env.sh"
setup_kubeconfig

kubectl get nodes
kubectl describe nodes | grep -E 'DiskPressure|Taints:|Filesystem' || true

if ! kubectl -n calendar-backend get deploy calendar-mongodb >/dev/null 2>&1; then
  kubectl -n calendar-backend delete pvc calendar-mongodb-data --ignore-not-found
fi

kubectl delete pods -A --field-selector=status.phase=Failed --ignore-not-found || true
kubectl delete pods -A --field-selector=status.phase=Succeeded --ignore-not-found || true

IMG="$(kubectl -n calendar-backend get deploy calendar-backend -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
if [ -z "${IMG}" ]; then
  IMG="$(kubectl -n calendar-frontend get deploy calendar-frontend -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
fi
if [ -z "${IMG}" ]; then
  IMG="busybox:1.36"
fi

PRUNE="disk-prune-${GITHUB_RUN_ID:-$RANDOM}"
kubectl -n kube-system delete pod "${PRUNE}" --ignore-not-found
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${PRUNE}
  namespace: kube-system
spec:
  restartPolicy: Never
  hostPID: true
  hostNetwork: true
  priorityClassName: system-node-critical
  tolerations:
    - operator: Exists
  containers:
    - name: prune
      image: ${IMG}
      imagePullPolicy: IfNotPresent
      securityContext:
        privileged: true
      command:
        - sh
        - -c
        - |
          nsenter -t 1 -m -- sh -c '
            (k3s crictl rmi --prune || crictl rmi --prune || true)
            (journalctl --vacuum-size=16M || true)
            rm -rf /var/log/*.gz /var/log/*.1 2>/dev/null || true
            df -h
          '
EOF

kubectl -n kube-system wait --for=jsonpath='{.status.phase}'=Succeeded pod/"${PRUNE}" --timeout=180s || kubectl -n kube-system logs "${PRUNE}" || true
kubectl -n kube-system delete pod "${PRUNE}" --ignore-not-found
echo "Disk relief finished"

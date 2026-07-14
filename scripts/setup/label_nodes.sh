#!/usr/bin/env bash
set -euo pipefail
if [ -z "${KUBECONFIG:-}" ] && [ -f "${HOME}/.kube/config" ]; then
  export KUBECONFIG="${HOME}/.kube/config"
fi

usage() {
  cat <<'EOF'
Usage:
  ./scripts/setup/label_nodes.sh node1=node-name-1 node2=node-name-2 ...

The left side is the short label used by artifact commands. The right side is
the Kubernetes node name from `kubectl get nodes`.
EOF
}

if [ "$#" -eq 0 ]; then
  usage
  exit 1
fi

for pair in "$@"; do
  short="${pair%%=*}"
  node="${pair#*=}"
  if [ -z "$short" ] || [ -z "$node" ] || [ "$short" = "$node" ]; then
    echo "bad mapping: $pair" >&2
    exit 1
  fi
  kubectl label node "$node" "short=$short" --overwrite
  if ssh "$short" test -e /dev/kvm >/dev/null 2>&1 || [ -e /dev/kvm ]; then
    kubectl label node "$node" "kvm=true" --overwrite
  fi
done

kubectl get nodes --show-labels

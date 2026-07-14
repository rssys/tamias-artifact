#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  sudo ./scripts/setup/join_k3s_worker.sh --server https://CONTROL_IP:6443 --token K3S_NODE_TOKEN
EOF
}

SERVER=""
TOKEN=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

if [ -z "$SERVER" ] || [ -z "$TOKEN" ]; then
  usage
  exit 1
fi

sudo mkdir -p /data/k3s
curl -sfL https://get.k3s.io | K3S_URL="$SERVER" K3S_TOKEN="$TOKEN" sh -s - agent --data-dir /data/k3s
echo "worker joined: $(hostname)"

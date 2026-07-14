#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/setup/sync_workers.sh --root /data/tamias-artifact --nodes node1,node2,...

Copies the script/config portion of this repository to the same path on each
worker. k3s hostPath volumes are local to each worker, so fuzzing jobs need the
config files to exist on every worker node.
EOF
}

ROOT="/data/tamias-artifact"
NODES=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --nodes) NODES="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

if [ -z "$NODES" ]; then
  usage
  exit 1
fi

IFS=',' read -r -a NODE_ARRAY <<< "$NODES"
for node in "${NODE_ARRAY[@]}"; do
  echo "syncing ${node}:${ROOT}"
  ssh -o StrictHostKeyChecking=no "$node" "sudo mkdir -p '$ROOT' && sudo chown -R $(id -u):$(id -g) '$ROOT'"
  rsync -az --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude 'workdir/' \
    --exclude 'results/' \
    --exclude 'runs/*.json' \
    ./ "$node:$ROOT/"
done

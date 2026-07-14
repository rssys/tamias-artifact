#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/setup/enable_kvm_vmware_backdoor.sh --nodes node1,node2,...

Reloads kvm/kvm-intel with enable_vmware_backdoor=y on each worker. Tamias'
QEMU-Nyx runner requires this KVM option.
EOF
}

NODES=""
while [ "$#" -gt 0 ]; do
  case "$1" in
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
  echo "configuring KVM on ${node}"
  ssh -o StrictHostKeyChecking=no "$node" '
    set -e
    sudo modprobe -r kvm-intel 2>/dev/null || true
    sudo modprobe -r kvm 2>/dev/null || true
    sudo modprobe kvm enable_vmware_backdoor=y
    sudo modprobe kvm-intel
    cat /sys/module/kvm/parameters/enable_vmware_backdoor
  '
done


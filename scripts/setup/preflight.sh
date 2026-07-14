#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-/data/tamias-artifact}"
if [ -z "${KUBECONFIG:-}" ] && [ -f "${HOME}/.kube/config" ]; then
  export KUBECONFIG="${HOME}/.kube/config"
fi

echo "Tamias AE preflight"
echo "root: ${ROOT}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "MISSING: $1"
    return 1
  fi
  echo "ok: $1 -> $(command -v "$1")"
}

missing=0
for cmd in python3 ssh scp sudo rsync; do
  need_cmd "$cmd" || missing=1
done

if command -v kubectl >/dev/null 2>&1; then
  echo "ok: kubectl -> $(command -v kubectl)"
  kubectl version --client=true >/dev/null 2>&1 || true
else
  echo "MISSING: kubectl (needed after k3s installation)"
fi

if [ -e /dev/kvm ]; then
  echo "ok: /dev/kvm exists"
  if [ -r /sys/module/kvm/parameters/enable_vmware_backdoor ]; then
    if grep -qi '^Y' /sys/module/kvm/parameters/enable_vmware_backdoor; then
      echo "ok: kvm enable_vmware_backdoor=Y"
    else
      echo "WARN: kvm enable_vmware_backdoor is not enabled; run scripts/setup/enable_kvm_vmware_backdoor.sh on workers"
    fi
  fi
else
  echo "MISSING: /dev/kvm (Tamias jobs need KVM on worker nodes)"
  missing=1
fi

if [ -e /dev/net/tun ]; then
  echo "ok: /dev/net/tun exists"
else
  echo "MISSING: /dev/net/tun (DistFuzz baseline jobs need it)"
fi

root_source="$(findmnt -rn -T / -o SOURCE 2>/dev/null || true)"
data_source="$(findmnt -rn -T /data -o SOURCE 2>/dev/null || true)"
if [ -n "$root_source" ] && [ "$root_source" = "$data_source" ]; then
  echo "WARN: /data is on the root filesystem; on CloudLab, run scripts/setup/setup_cloudlab_data_disk.sh before long runs"
elif [ -n "$data_source" ]; then
  echo "ok: /data is mounted on ${data_source}"
fi

mkdir -p "${ROOT}/workdir" "${ROOT}/runs" "${ROOT}/results"
test -w "${ROOT}/workdir" || { echo "MISSING: ${ROOT}/workdir is not writable"; missing=1; }
echo "ok: root directories exist"

if [ "$missing" -ne 0 ]; then
  echo "Preflight found blocking issues."
  exit 1
fi

echo "Preflight passed."

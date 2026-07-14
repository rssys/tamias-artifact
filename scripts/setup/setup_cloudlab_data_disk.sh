#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/setup/setup_cloudlab_data_disk.sh --nodes node0,node1,node2 [--device /dev/sdb]

CloudLab machines usually provide a large data disk at /dev/sdb. This script
mounts that disk at /data on each listed machine. Run it before installing k3s
or before syncing the artifact to worker machines.

WARNING: if the device has no filesystem, this script formats it with ext4.
Use it only on the empty CloudLab data disk.

Options:
  --nodes        Comma-separated machine names.
  --device       Block device to format and mount. Default: /dev/sdb.
  --mountpoint   Mount point. Default: /data.
  --owner        Owner for the mount point. Default: current user and group.
EOF
}

NODES=""
DEVICE="/dev/sdb"
MOUNTPOINT="/data"
OWNER="$(id -u):$(id -g)"
ALLOW_NONEMPTY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --nodes) NODES="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --mountpoint) MOUNTPOINT="$2"; shift 2 ;;
    --owner) OWNER="$2"; shift 2 ;;
    --allow-nonempty) ALLOW_NONEMPTY=1; shift ;;
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
  echo "setting up ${MOUNTPOINT} on ${node}"
  ssh -o StrictHostKeyChecking=no "$node" \
    sudo bash -s -- "$DEVICE" "$MOUNTPOINT" "$OWNER" "$ALLOW_NONEMPTY" <<'REMOTE'
set -euo pipefail

DEVICE="$1"
MOUNTPOINT="$2"
OWNER="$3"
ALLOW_NONEMPTY="$4"

if mountpoint -q "$MOUNTPOINT"; then
  echo "$MOUNTPOINT already mounted on $(findmnt -n -o SOURCE --target "$MOUNTPOINT")"
  mkdir -p "$MOUNTPOINT/k3s" "$MOUNTPOINT/tamias-artifact"
  chown -R "$OWNER" "$MOUNTPOINT"
  df -h "$MOUNTPOINT"
  exit 0
fi

if [ ! -b "$DEVICE" ]; then
  echo "missing block device: $DEVICE" >&2
  exit 1
fi

if [ -d "$MOUNTPOINT" ] && [ -n "$(find "$MOUNTPOINT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ] && [ "$ALLOW_NONEMPTY" != "1" ]; then
  echo "$MOUNTPOINT is not empty. Move existing files or rerun with --allow-nonempty." >&2
  exit 1
fi

FSTYPE="$(blkid -o value -s TYPE "$DEVICE" 2>/dev/null || true)"
if [ -z "$FSTYPE" ]; then
  echo "WARNING: formatting $DEVICE. This destroys data on that device." >&2
  mkfs.ext4 -F "$DEVICE"
elif [ "$FSTYPE" != "ext4" ]; then
  echo "$DEVICE has filesystem type $FSTYPE; expected ext4 or empty device" >&2
  exit 1
fi

mkdir -p "$MOUNTPOINT"
mount "$DEVICE" "$MOUNTPOINT"
UUID="$(blkid -o value -s UUID "$DEVICE")"
if ! grep -q "UUID=${UUID}[[:space:]]${MOUNTPOINT}[[:space:]]" /etc/fstab; then
  printf 'UUID=%s %s ext4 defaults,nofail 0 2\n' "$UUID" "$MOUNTPOINT" >> /etc/fstab
fi

mkdir -p "$MOUNTPOINT/k3s" "$MOUNTPOINT/tamias-artifact"
chown -R "$OWNER" "$MOUNTPOINT"
df -h "$MOUNTPOINT"
REMOTE
done

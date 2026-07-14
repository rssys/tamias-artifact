#!/usr/bin/env bash
set -euo pipefail

sudo mkdir -p /data/k3s

if command -v k3s >/dev/null 2>&1 && sudo k3s kubectl get nodes >/dev/null 2>&1; then
  echo "k3s server already appears to be running."
else
  curl -sfL https://get.k3s.io | sh -s - server --disable traefik --data-dir /data/k3s
fi

mkdir -p "${HOME}/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "${HOME}/.kube/config"
sudo chown "$(id -u)":"$(id -g)" "${HOME}/.kube/config"
chmod 600 "${HOME}/.kube/config"

echo "k3s server ready."
echo "Node token: sudo cat /data/k3s/server/node-token"
echo "Server URL for workers: https://$(hostname -I | awk '{print $1}'):6443"

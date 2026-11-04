#!/usr/bin/env bash
# Create a Debian 12 LXC on a Proxmox host with one NIC per lighting network
# and install ArtBridge in it. Run this ON THE PROXMOX HOST as root.
#
# Example (console on VLAN 10 as 2.x, stage on VLAN 20 as 10.20.30.x, plus a
# management NIC on DHCP so the container can reach the internet for updates):
#
#   CTID=210 \
#   NET0="bridge=vmbr0,tag=10,ip=2.0.0.10/8" \
#   NET1="bridge=vmbr0,tag=20,ip=10.20.30.5/24" \
#   MGMT="bridge=vmbr0,ip=dhcp" \
#   ./proxmox-create-lxc.sh
#
# Every setting is an environment variable with a default below. Untagged
# ports: leave out "tag=". A gateway for apt: add ",gw=192.168.1.1" to MGMT or
# any NET. Without a route to the internet the install step will fail; you can
# then copy the repo in and run deploy/install.sh by hand.

set -euo pipefail

CTID="${CTID:-210}"
HOSTNAME_="${CT_HOSTNAME:-artbridge}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
DISK_GB="${DISK_GB:-4}"
MEMORY="${MEMORY:-512}"
CORES="${CORES:-1}"
NET0="${NET0:-bridge=vmbr0,ip=2.0.0.10/8}"
NET1="${NET1:-bridge=vmbr0,ip=10.20.30.5/24}"
MGMT="${MGMT:-}"
PASSWORD="${PASSWORD:-}"
SSH_KEY="${SSH_KEY:-}"
BRANCH="${ARTBRIDGE_BRANCH:-main}"

if ! command -v pct >/dev/null; then
  echo "pct not found: run this on the Proxmox host" >&2
  exit 1
fi
if pct status "$CTID" >/dev/null 2>&1; then
  echo "container $CTID already exists; pick another CTID" >&2
  exit 1
fi

echo "updating template list"
pveam update >/dev/null
TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -1)"
if [[ -z "$TEMPLATE" ]]; then
  echo "no debian-12-standard template available" >&2
  exit 1
fi
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
  echo "downloading $TEMPLATE"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

ARGS=(
  --hostname "$HOSTNAME_"
  --cores "$CORES" --memory "$MEMORY" --swap 0
  --rootfs "$STORAGE:$DISK_GB"
  --unprivileged 1
  --onboot 1
  --features nesting=0
  --net0 "name=eth0,$NET0"
  --net1 "name=eth1,$NET1"
)
if [[ -n "$MGMT" ]]; then
  ARGS+=(--net2 "name=eth2,$MGMT")
fi
if [[ -n "$PASSWORD" ]]; then
  ARGS+=(--password "$PASSWORD")
fi
if [[ -n "$SSH_KEY" ]]; then
  ARGS+=(--ssh-public-keys "$SSH_KEY")
fi

echo "creating container $CTID ($HOSTNAME_)"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" "${ARGS[@]}"
pct start "$CTID"
echo "waiting for the container to come up"
sleep 5

echo "installing ArtBridge"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null && curl -fsSL https://raw.githubusercontent.com/danieltrillo/artnet-bridge/$BRANCH/deploy/install.sh | ARTBRIDGE_BRANCH=$BRANCH bash"

echo
echo "done. Edit the config with:  pct exec $CTID -- nano /etc/artbridge/config.yaml"
echo "or open the web UI on port 8080 of any of the container's addresses:"
pct exec "$CTID" -- artbridge interfaces | sed 's/^/  /'

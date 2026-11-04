#!/usr/bin/env bash
# Install or update ArtBridge on Debian 12/13 (or Ubuntu 22.04+).
#
#   From a checkout:  sudo ./deploy/install.sh
#   From anywhere:    curl -fsSL https://raw.githubusercontent.com/danieltrillo/artnet-bridge/main/deploy/install.sh | sudo bash
#
# What it does: installs python3 + python3-yaml + python3-aiohttp, copies the
# code to /opt/artbridge, creates /etc/artbridge/config.yaml from the example
# if there is none, and enables the systemd service.

set -euo pipefail

REPO_URL="${ARTBRIDGE_REPO:-https://github.com/danieltrillo/artnet-bridge.git}"
BRANCH="${ARTBRIDGE_BRANCH:-main}"
INSTALL_DIR=/opt/artbridge
CONFIG_DIR=/etc/artbridge
SERVICE=artbridge

if [[ $EUID -ne 0 ]]; then
  echo "please run as root (sudo)" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-yaml python3-aiohttp git ca-certificates >/dev/null

# Where is the source? A checkout next to this script, or a fresh clone.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
SRC=""
if [[ -n "$SCRIPT_DIR" && -d "$SCRIPT_DIR/../artbridge" ]]; then
  SRC="$(cd "$SCRIPT_DIR/.." && pwd)"
  echo "installing from checkout $SRC"
else
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  echo "cloning $REPO_URL ($BRANCH)"
  git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$TMP/src"
  SRC="$TMP/src"
fi

id -u $SERVICE >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin $SERVICE

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"
rm -rf "$INSTALL_DIR/artbridge"
cp -r "$SRC/artbridge" "$INSTALL_DIR/artbridge"
cp "$SRC/README.md" "$INSTALL_DIR/README.md" 2>/dev/null || true
find "$INSTALL_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +

if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  cp "$SRC/artbridge/example-config.yaml" "$CONFIG_DIR/config.yaml"
  echo "created $CONFIG_DIR/config.yaml from the example; edit it (or use the web UI)"
fi
chown -R $SERVICE:$SERVICE "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
chmod 640 "$CONFIG_DIR/config.yaml"

cat > /usr/local/bin/artbridge <<'EOF'
#!/bin/sh
PYTHONPATH=/opt/artbridge exec /usr/bin/python3 -m artbridge "$@"
EOF
chmod 755 /usr/local/bin/artbridge

cp "$SRC/deploy/artbridge.service" /etc/systemd/system/artbridge.service
systemctl daemon-reload
systemctl enable artbridge >/dev/null 2>&1
systemctl restart artbridge

sleep 1
if systemctl is-active --quiet artbridge; then
  PORT="$(python3 - <<'EOF'
import yaml
try:
    print(yaml.safe_load(open("/etc/artbridge/config.yaml")).get("web", {}).get("port", 8080))
except Exception:
    print(8080)
EOF
)"
  echo
  echo "ArtBridge is running."
  echo "  config:  $CONFIG_DIR/config.yaml   (artbridge check -c $CONFIG_DIR/config.yaml)"
  echo "  logs:    journalctl -u artbridge -f"
  echo "  web UI:  http://<this-host>:$PORT"
  echo "  this host's addresses:"
  artbridge interfaces | sed 's/^/    /'
else
  echo "ArtBridge did not start. See: journalctl -u artbridge -n 50" >&2
  exit 1
fi

#!/usr/bin/env bash
# Margin — self-hosted read-later server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Install Margin as a systemd service on Ubuntu (20.04+). Run as root:
#
#   sudo bash deploy/install.sh
#
# Idempotent — safe to re-run after pulling updates.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/margin}"
OUTPUT_DIR="${OUTPUT_DIR:-/var/lib/margin/inbox}"
SERVICE_USER="${SERVICE_USER:-margin}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update
apt-get install -y python3-venv python3-pip rsync
# Pandoc is optional (companion .tex/.org files); ignore failure on minimal repos
apt-get install -y pandoc || echo "pandoc not installed — .tex/.org output will be skipped"

echo "==> Creating service user '$SERVICE_USER'"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  # Real home dir: Playwright stores its Chromium build in ~/.cache
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Copying application to $APP_DIR"
mkdir -p "$APP_DIR" "$OUTPUT_DIR"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
  --exclude '.env' --exclude 'server.log' \
  "$REPO_DIR/" "$APP_DIR/"
[ -f "$APP_DIR/.env" ] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"

echo "==> Creating virtualenv and installing Python dependencies"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

echo "==> Installing headless Chromium (+ system dependencies) for Playwright"
# --with-deps needs root; the browser itself must belong to the service user
"$APP_DIR/.venv/bin/playwright" install-deps chromium
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/playwright" install chromium

echo "==> Setting ownership"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$(dirname "$OUTPUT_DIR")"

echo "==> Installing systemd unit"
sed -e "s|/opt/margin|$APP_DIR|g" \
    -e "s|/var/lib/margin/inbox|$OUTPUT_DIR|g" \
    -e "s|ReadWritePaths=/var/lib/margin|ReadWritePaths=$(dirname "$OUTPUT_DIR")|" \
    -e "s|^User=.*|User=$SERVICE_USER|" \
    -e "s|^Group=.*|Group=$SERVICE_USER|" \
    "$REPO_DIR/deploy/margin.service" \
    > /etc/systemd/system/margin.service
systemctl daemon-reload
systemctl enable --now margin

sleep 2
systemctl --no-pager status margin || true
echo
echo "Done. Verify with:  curl http://localhost:8000/health"
echo "Saved files land in: $OUTPUT_DIR"
echo "Logs:               journalctl -u margin -f"

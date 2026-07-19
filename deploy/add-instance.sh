#!/usr/bin/env bash
# Margin — self-hosted read-later server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Create (or repair) a per-person Margin instance: writes
# /etc/margin/<user>.env, installs headless Chromium for that user, and
# enables the margin@<user> service. Run as root:
#
#   sudo bash deploy/add-instance.sh <user> <port> [output-dir]
#
# Defaults: output-dir = /home/<user>/ReadLater/inbox; a MARGIN_TOKEN is
# generated on first run. Edit the env file to change anything, then
# `systemctl restart margin@<user>`. Idempotent — an existing env file is
# kept untouched.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/margin}"
USAGE="usage: sudo bash deploy/add-instance.sh <user> <port> [output-dir]"
USER_NAME="${1:?$USAGE}"
PORT="${2:?$USAGE}"
OUT="${3:-/home/$USER_NAME/ReadLater/inbox}"
ENV_FILE="/etc/margin/$USER_NAME.env"

if [ "$(id -u)" -ne 0 ]; then
  echo "$USAGE  (must run as root)" >&2
  exit 1
fi
id -u "$USER_NAME" >/dev/null   # errors out if the user doesn't exist
GROUP_NAME="$(id -gn "$USER_NAME")"

mkdir -p /etc/margin
if [ -f "$ENV_FILE" ]; then
  echo "==> $ENV_FILE exists — keeping it (edit it + restart to change settings)"
else
  echo "==> Writing $ENV_FILE"
  TOKEN="$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  cat > "$ENV_FILE" <<EOF
HOST=0.0.0.0
PORT=$PORT
OUTPUT_DIR=$OUT
MARGIN_TOKEN=$TOKEN
EOF
  chmod 600 "$ENV_FILE"
fi

echo "==> Creating output directory $OUT"
install -d -o "$USER_NAME" -g "$GROUP_NAME" "$OUT"

echo "==> Installing headless Chromium for $USER_NAME"
sudo -u "$USER_NAME" "$APP_DIR/.venv/bin/playwright" install chromium

echo "==> Enabling margin@$USER_NAME"
systemctl daemon-reload
systemctl enable --now "margin@$USER_NAME"
sleep 2
systemctl --no-pager status "margin@$USER_NAME" || true

echo
echo "Instance ready:"
echo "  Queue URL:  http://<server>:$PORT/  (open once with ?token=<token> to store the cookie)"
echo "  Output dir: $OUT"
echo "  Config:     $ENV_FILE  (restart margin@$USER_NAME after edits)"
echo "  Token:      $(grep '^MARGIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
echo "  Logs:       journalctl -u margin@$USER_NAME -f"

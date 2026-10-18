#!/usr/bin/env bash
# Mimic installer / updater for Ubuntu 22.04. Safe to re-run (it is also what `mimic-update` runs).
# Only touches: /opt/mimic, /var/lib/mimic, /etc/mimic.env, the mimic-bot service and two helpers.
# Uses Telegram long polling: no ports, no Caddy changes, does not touch other services.
set -euo pipefail

REPO_URL="${MIMIC_REPO:-https://github.com/Jparker9204/mimic.git}"
BRANCH="${MIMIC_BRANCH:-main}"
APP=/opt/mimic
DATA=/var/lib/mimic
ENVF=/etc/mimic.env
SVC=mimic-bot

if [ "$(id -u)" -ne 0 ]; then echo "Please run as root (prefix with sudo)."; exit 1; fi

echo "==> Installing packages (git, python venv)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv >/dev/null

echo "==> Service user and data folder"
id mimic >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin mimic
install -d -o mimic -g mimic -m 750 "$DATA"

echo "==> Getting the code into $APP"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q origin "$BRANCH"
  git -C "$APP" reset -q --hard "origin/$BRANCH"
else
  rm -rf "$APP"
  git clone -q -b "$BRANCH" "$REPO_URL" "$APP"
fi
echo "    version: $(git -C "$APP" log -1 --format='%h %s')"

echo "==> Python virtualenv"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$ENVF" ]; then
  echo "==> Creating $ENVF (token not set yet)"
  install -m 600 -o root -g root /dev/null "$ENVF"
  echo "# Mimic settings. Set the token with: sudo mimic-set-token" > "$ENVF"
fi
chmod 600 "$ENVF"; chown root:root "$ENVF"

echo "==> systemd service $SVC"
cat > /etc/systemd/system/$SVC.service <<UNIT
[Unit]
Description=Mimic Telegram bot (@Mimic_TG_Bot)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mimic
Group=mimic
WorkingDirectory=$APP
Environment=MIMIC_DB=$DATA/mimic.db
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=$ENVF
ExecStart=$APP/.venv/bin/python -m mimic
Restart=always
RestartSec=10
# keep room for PaperPump on a 1 GB droplet
MemoryMax=300M
CPUQuota=70%
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
UNIT

echo "==> Helper commands"
cat > /usr/local/bin/mimic-set-token <<'HELPER'
#!/usr/bin/env bash
# Prompt for the Telegram bot token (hidden), save it to /etc/mimic.env (chmod 600), restart Mimic.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then exec sudo "$0" "$@"; fi
ENVF=/etc/mimic.env
read -rsp "Paste the bot token from @BotFather (input hidden): " TOKEN; echo
if ! [[ "$TOKEN" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]]; then echo "That doesn't look like a bot token. Nothing changed."; exit 1; fi
read -rsp "Optional private Solana RPC URL (input hidden, press Enter to skip): " RPC; echo
TMP=$(mktemp); chmod 600 "$TMP"
{
  grep -v -E '^(MIMIC_BOT_TOKEN|MIMIC_RPC_URL)=' "$ENVF" 2>/dev/null || true
  echo "MIMIC_BOT_TOKEN=$TOKEN"
  if [ -n "$RPC" ]; then echo "MIMIC_RPC_URL=$RPC"; else grep -E '^MIMIC_RPC_URL=' "$ENVF" 2>/dev/null || true; fi
} > "$TMP"
install -m 600 -o root -g root "$TMP" "$ENVF"; rm -f "$TMP"
unset TOKEN RPC
systemctl enable mimic-bot >/dev/null 2>&1
systemctl restart mimic-bot
sleep 4
if systemctl is-active --quiet mimic-bot; then echo "✅ Mimic is running. Open Telegram and send /start to your bot."
else echo "⚠️ Mimic did not start. Check: journalctl -u mimic-bot -n 30 --no-pager"; fi
HELPER
cat > /usr/local/bin/mimic-update <<'HELPER'
#!/usr/bin/env bash
# Pull the latest Mimic code and restart (re-runs the installer, which is idempotent).
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then exec sudo "$0" "$@"; fi
bash /opt/mimic/deploy/install.sh
HELPER
chmod 755 /usr/local/bin/mimic-set-token /usr/local/bin/mimic-update

systemctl daemon-reload
if grep -q '^MIMIC_BOT_TOKEN=.\+' "$ENVF"; then
  systemctl enable mimic-bot >/dev/null 2>&1
  systemctl restart mimic-bot
  sleep 3
  systemctl is-active --quiet mimic-bot && echo "✅ mimic-bot restarted and running." \
    || echo "⚠️ mimic-bot failed to start: journalctl -u mimic-bot -n 30 --no-pager"
else
  echo ""
  echo "✅ Installed. Next step: run   mimic-set-token   and paste your bot token."
fi

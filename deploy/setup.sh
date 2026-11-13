#!/usr/bin/env bash
# Install or update the app on a fresh Ubuntu 24.04 server. Safe to re-run.
#
#   sudo bash setup.sh <domain> <allowed-origin>
#   e.g. sudo bash setup.sh 13-200-1-2.sslip.io https://meeting-summarizer.vercel.app
#
# The Groq key lives in /etc/meeting-summarizer/secrets.env (GROQ_API_KEY=...),
# which this script never touches.
set -euo pipefail

DOMAIN="$1"
ORIGIN="${2:-}"
REPO="https://github.com/colin-110/meeting-summarizer.git"
APP_DIR=/opt/meeting-summarizer
DATA_DIR=/var/lib/meeting-summarizer
CONF_DIR=/etc/meeting-summarizer

# 1 GB swap: 512 MB RAM is enough (the app peaks around 120 MB), swap is the safety net.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
  sysctl -w vm.swappiness=10 && echo "vm.swappiness=10" > /etc/sysctl.d/99-swap.conf
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv git caddy unattended-upgrades

id app >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin app
install -d -o app -g app "$DATA_DIR"
install -d -m 750 -o root -g app "$CONF_DIR"

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q origin main && git -C "$APP_DIR" reset -q --hard origin/main
else
  git clone -q "$REPO" "$APP_DIR"
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# Settings sized for 512 MB: one process runs both the API and one worker.
cat > "$CONF_DIR/app.env" <<EOF
ENV_FILE=
DATA_DIR=$DATA_DIR
RUN_WORKERS=true
WORKER_CONCURRENCY=1
TRANSCRIBE_PARALLELISM=2
TRUST_PROXY_HEADERS=true
ALLOWED_ORIGINS=$ORIGIN
EOF
touch "$CONF_DIR/secrets.env"
chown root:app "$CONF_DIR"/*.env && chmod 640 "$CONF_DIR"/*.env

cat > /etc/systemd/system/meeting-summarizer.service <<EOF
[Unit]
Description=Meeting Summarizer
After=network-online.target
Wants=network-online.target

[Service]
User=app
WorkingDirectory=$APP_DIR
EnvironmentFile=$CONF_DIR/app.env
EnvironmentFile=$CONF_DIR/secrets.env
ExecStart=$APP_DIR/.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3
MemoryMax=400M
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR /tmp
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# Caddy gets and renews the HTTPS certificate on its own.
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	reverse_proxy 127.0.0.1:8000
}
EOF

systemctl daemon-reload
systemctl enable -q meeting-summarizer caddy
systemctl restart meeting-summarizer
systemctl reload caddy || systemctl restart caddy
echo "Deployed: https://$DOMAIN"

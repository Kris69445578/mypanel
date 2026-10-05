#!/usr/bin/env bash
# MyPanel auto-installer (Ubuntu 22.04 / 24.04, run as root)
#
#   curl -sSL https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/mypanel/main/install.sh | sudo bash
#
# Optional environment variables:
#   DOMAIN=panel.example.com EMAIL=you@example.com   -> automatic HTTPS (Let's Encrypt)
#   ADMIN_USER=admin ADMIN_PASSWORD=...              -> first admin account (password is random if unset)
#   REPO_URL=https://github.com/USER/REPO.git BRANCH=main
#   APP_PORT=8000
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/YOUR_GITHUB_USERNAME/mypanel.git}"
BRANCH="${BRANCH:-main}"
APP_PORT="${APP_PORT:-8000}"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
ADMIN_USER="${ADMIN_USER:-admin}"
INSTALL_DIR=/opt/mypanel
CONF_DIR=/etc/mypanel
DATA_ROOT=/srv/mypanel
STATE_DIR=/var/lib/mypanel

log() { echo -e "\033[1;32m[mypanel]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
die() { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (use sudo)."
command -v apt-get >/dev/null || die "This installer needs apt (Ubuntu/Debian)."
. /etc/os-release 2>/dev/null || true
[ "${ID:-}" = "ubuntu" ] || warn "Built for Ubuntu 22.04; continuing on ${PRETTY_NAME:-unknown OS}."

export DEBIAN_FRONTEND=noninteractive
log "Installing system packages (docker, nginx, python)..."
apt-get update -y -qq
apt-get install -y -qq ca-certificates curl git openssl rsync nginx docker.io python3 python3-venv python3-pip
systemctl enable --now docker

# ---- get the source: local clone if present, otherwise GitHub ----
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
mkdir -p "$INSTALL_DIR"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/panel/main.py" ]; then
  log "Using local source at $SELF_DIR"
  [ "$SELF_DIR" = "$INSTALL_DIR" ] || rsync -a --delete --exclude venv --exclude .git "$SELF_DIR/" "$INSTALL_DIR/"
else
  case "$REPO_URL" in *YOUR_GITHUB_USERNAME*) die "Set REPO_URL=https://github.com/USER/REPO.git (see README).";; esac
  log "Cloning $REPO_URL ($BRANCH)..."
  TMP="$(mktemp -d)"
  git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$TMP"
  rsync -a --delete --exclude venv --exclude .git "$TMP/" "$INSTALL_DIR/"
  rm -rf "$TMP"
fi

log "Setting up Python environment..."
[ -d "$INSTALL_DIR/venv" ] || python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# ---- config (kept across updates) ----
mkdir -p "$CONF_DIR" "$DATA_ROOT/servers" "$STATE_DIR"
chmod 700 "$CONF_DIR"
FIRST=0
if [ ! -f "$CONF_DIR/panel.env" ]; then
  FIRST=1
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-18)}"
  cat > "$CONF_DIR/panel.env" <<ENV
PANEL_SECRET=$(openssl rand -hex 32)
PANEL_ADMIN_USER=$ADMIN_USER
PANEL_ADMIN_PASSWORD=$ADMIN_PASSWORD
PANEL_DATA=$DATA_ROOT/servers
PANEL_DB=$STATE_DIR/panel.db
ENV
  chmod 600 "$CONF_DIR/panel.env"
fi
cat > "$CONF_DIR/install.conf" <<CONF
REPO_URL=$REPO_URL
BRANCH=$BRANCH
CONF
chmod 600 "$CONF_DIR/install.conf"

# ---- systemd ----
cat > /etc/systemd/system/mypanel.service <<UNIT
[Unit]
Description=MyPanel
After=network.target docker.service
Requires=docker.service

[Service]
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONF_DIR/panel.env
ExecStart=$INSTALL_DIR/venv/bin/uvicorn panel.main:app --host 127.0.0.1 --port $APP_PORT
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable mypanel >/dev/null 2>&1
systemctl restart mypanel

# ---- nginx reverse proxy (WebSocket-ready) ----
cat > /etc/nginx/sites-available/mypanel <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name @SERVER_NAME@;
    client_max_body_size 20m;
    location / {
        proxy_pass http://127.0.0.1:@APP_PORT@;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
    }
}
NGINX
sed -i "s/@SERVER_NAME@/${DOMAIN:-_}/; s/@APP_PORT@/$APP_PORT/" /etc/nginx/sites-available/mypanel
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/mypanel /etc/nginx/sites-enabled/mypanel
nginx -t
systemctl enable nginx >/dev/null 2>&1
systemctl reload nginx || systemctl restart nginx

if [ -n "$DOMAIN" ] && [ -n "$EMAIL" ]; then
  log "Requesting HTTPS certificate for $DOMAIN..."
  apt-get install -y -qq certbot python3-certbot-nginx
  certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --non-interactive --redirect \
    || warn "Certbot failed. Check that $DOMAIN points to this server, then run: certbot --nginx -d $DOMAIN"
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

# ---- helper command ----
cat > /usr/local/bin/mypanel <<'CLI'
#!/usr/bin/env bash
set -e
case "${1:-}" in
  status)  systemctl status mypanel --no-pager ;;
  logs)    journalctl -u mypanel -f ;;
  restart) systemctl restart mypanel ;;
  update)
    . /etc/mypanel/install.conf
    T="$(mktemp -d)"; git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$T"
    bash "$T/install.sh"; rm -rf "$T" ;;
  reset-password)
    shift; set -a; . /etc/mypanel/panel.env; set +a
    cd /opt/mypanel && ./venv/bin/python -m panel.cli reset-password "$@" ;;
  uninstall) bash /opt/mypanel/uninstall.sh ;;
  *) echo "usage: mypanel {status|logs|restart|update|reset-password <user> <pass>|uninstall}" ;;
esac
CLI
chmod +x /usr/local/bin/mypanel

# ---- health check ----
log "Waiting for the panel to start..."
for i in $(seq 1 30); do
  curl -fs "http://127.0.0.1:$APP_PORT/api/health" >/dev/null 2>&1 && OK=1 && break
  sleep 1
done
[ "${OK:-0}" = "1" ] || { journalctl -u mypanel -n 30 --no-pager; die "Panel did not start. See logs above."; }

if [ "$FIRST" = "1" ]; then
  # admin account now exists in the database; drop the plaintext password from disk
  sed -i '/^PANEL_ADMIN_PASSWORD=/d' "$CONF_DIR/panel.env"
fi

IP="$(curl -fs --max-time 4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
URL="http://$IP"; [ -n "$DOMAIN" ] && URL="https://$DOMAIN"
echo
log "MyPanel is running  ->  $URL"
if [ "$FIRST" = "1" ]; then
  echo "    Username: $ADMIN_USER"
  echo "    Password: $ADMIN_PASSWORD"
  echo "    (save it now - it is not stored on disk; change it in the Account page)"
fi
[ -n "$DOMAIN" ] || warn "No DOMAIN set: logins travel over plain HTTP. Re-run with DOMAIN=... EMAIL=... for HTTPS."
echo "    Manage with: mypanel {status|logs|restart|update|reset-password|uninstall}"

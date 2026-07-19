#!/usr/bin/env bash
# Deploy the trading agent to a fresh Ubuntu/Debian VPS as a systemd service.
#
# Usage: sudo ./deploy.sh
# Safe to re-run: pulls latest code, reinstalls deps, and restarts the service.
#
# Override any of these via environment variables before running, e.g.:
#   SERVICE_MODE=paper REPO_BRANCH=develop sudo -E ./deploy.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/shibilshamz/stock-trading-agent-v3.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/trading-agent}"
SERVICE_NAME="${SERVICE_NAME:-trading-agent}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-root}}"
SERVICE_MODE="${SERVICE_MODE:-dashboard}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"

export DEBIAN_FRONTEND=noninteractive

log()  { echo -e "\n\033[1;32m==>\033[0m $1"; }
warn() { echo -e "\033[1;33mWARNING:\033[0m $1"; }
die()  { echo -e "\033[1;31mERROR:\033[0m $1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "This script must be run as root (e.g. sudo ./deploy.sh)."
command -v apt-get >/dev/null 2>&1 || die "This script targets Debian/Ubuntu (apt-get not found)."

log "Updating system packages"
apt-get update -y
apt-get upgrade -y

log "Installing prerequisites"
apt-get install -y software-properties-common curl git build-essential

log "Installing Python ${PYTHON_VERSION}"
if ! command -v "python${PYTHON_VERSION}" >/dev/null 2>&1; then
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y
  apt-get install -y "python${PYTHON_VERSION}" "python${PYTHON_VERSION}-venv" "python${PYTHON_VERSION}-dev"
fi

# Not currently used by the app itself (the dashboard frontend is plain JS,
# no build step) -- installed because the deployment spec asks for it,
# e.g. for future frontend tooling. Safe to remove this block if unneeded.
log "Installing Node.js (LTS)"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
  apt-get install -y nodejs
fi

log "Installing SQLite"
apt-get install -y sqlite3 libsqlite3-dev

log "Cloning/updating repository at ${INSTALL_DIR}"
mkdir -p "$INSTALL_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  warn "$INSTALL_DIR already contains a git repo; pulling latest instead of cloning."
  sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" fetch origin
  sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
  sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull origin "$REPO_BRANCH"
else
  sudo -u "$SERVICE_USER" git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

log "Creating virtual environment"
sudo -u "$SERVICE_USER" "python${PYTHON_VERSION}" -m venv "$INSTALL_DIR/venv"

log "Installing Python dependencies"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

log "Initializing database"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/database/init_db.py"

log "Setting up .env"
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  chown "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
  warn ".env created from .env.example -- edit $INSTALL_DIR/.env and fill in GROQ_API_KEY, TELEGRAM_BOT_TOKEN, and TELEGRAM_CHAT_ID before those features will work."
else
  warn ".env already exists, leaving it untouched."
fi

log "Creating systemd service (mode=${SERVICE_MODE})"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Trading Agent (${SERVICE_MODE})
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/main.py --mode ${SERVICE_MODE} --config ${INSTALL_DIR}/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

log "Starting and enabling service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

log "Deployment complete. Service status:"
systemctl status "$SERVICE_NAME" --no-pager || true

log "Last 50 log lines:"
journalctl -u "$SERVICE_NAME" -n 50 --no-pager

echo -e "\nNext steps:"
echo "  - Edit ${INSTALL_DIR}/.env with your real API keys, then: systemctl restart ${SERVICE_NAME}"
echo "  - Follow live logs:  journalctl -u ${SERVICE_NAME} -f"
echo "  - Check status:      systemctl status ${SERVICE_NAME}"
if [[ "$SERVICE_MODE" == "dashboard" ]]; then
  echo "  - Dashboard should be reachable at: http://<server-ip>:8000"
fi

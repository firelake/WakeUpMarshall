#!/usr/bin/env bash
#
# WakeUpMarshall - one-command installer for Linux (BlueZ / bluetoothctl).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/firelake/WakeUpMarshall/main/install.sh | bash
#   bash install.sh [local-repo-path]     # install from a local checkout
#
set -euo pipefail

APP="wakeupmarshall"
BASE_DIR="${HOME}/.wakeupmarshall"
APP_DIR="${BASE_DIR}/app"
VENV_DIR="${BASE_DIR}/venv"
REPO_URL="https://github.com/firelake/WakeUpMarshall.git"
SRC_DIR="${1:-}"

info()  { printf "\033[1;36m[%s]\033[0m %s\n" "$APP" "$*"; }
die()   { printf "\033[1;31m[%s] ERROR:\033[0m %s\n" "$APP" "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 is required (install via apt/dnf/pacman)."
command -v git   >/dev/null 2>&1 || die "git is required."
command -v bluetoothctl >/dev/null 2>&1 || info "NOTE: bluetoothctl (BlueZ) not found - the app will try the 'bleak' backend."

mkdir -p "$BASE_DIR"

if [ -n "$SRC_DIR" ]; then
    info "Installing from local checkout: $SRC_DIR"
    rm -rf "$APP_DIR"
    mkdir -p "$APP_DIR"
    # Copy sources without build/runtime junk (cp does not honour .gitignore).
    (cd "$SRC_DIR" && tar --exclude='.git' --exclude='.venv' --exclude='venv' \
        --exclude='__pycache__' --exclude='*.egg-info' --exclude='.wumtest*' \
        --exclude='*.log' -cf - .) | (cd "$APP_DIR" && tar -xf -)
else
    if [ ! -d "$APP_DIR/.git" ]; then
        info "Cloning repository..."
        git clone --depth 1 "$REPO_URL" "$APP_DIR"
    else
        info "Updating repository..."
        git -C "$APP_DIR" pull --ff-only || true
    fi
fi

info "Creating virtualenv..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "$APP_DIR"

SERVICE_FILE="${HOME}/.config/systemd/user/${APP}.service"
if systemctl --user list-units >/dev/null 2>&1; then
    info "Installing systemd user service..."
    mkdir -p "${HOME}/.config/systemd/user"
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=WakeUpMarshall - wake Marshall speakers on a schedule
After=bluetooth.service

[Service]
Type=simple
ExecStart=${VENV_DIR}/bin/${APP} serve --no-open
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "${APP}.service" >/dev/null 2>&1 || true
    if systemctl --user is-active --quiet "${APP}.service"; then
        info "systemd user service installed & started."
        URL="http://127.0.0.1:8756"
    else
        info "systemd service did not start - falling back to nohup."
        pkill -f "${VENV_DIR}/bin/${APP} serve" >/dev/null 2>&1 || true
        nohup "${VENV_DIR}/bin/${APP}" serve --no-open >"${BASE_DIR}/server.log" 2>&1 &
        sleep 2
        URL="http://127.0.0.1:8756"
    fi
else
    info "systemd user session not available - starting in background with nohup."
    pkill -f "${VENV_DIR}/bin/${APP} serve" >/dev/null 2>&1 || true
    nohup "${VENV_DIR}/bin/${APP}" serve --no-open >"${BASE_DIR}/server.log" 2>&1 &
    sleep 2
    URL="http://127.0.0.1:8756"
fi

echo
info "Install complete."
echo "  Web UI   : $URL"
echo "  Data dir : $BASE_DIR"
echo "  CLI      : ${VENV_DIR}/bin/${APP} --help"
echo "  Logs     : ${BASE_DIR}/server.log (or: journalctl --user -u ${APP})"
echo "  Stop     : systemctl --user stop ${APP}   (or: pkill -f ${APP} serve)"

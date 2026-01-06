#!/bin/bash
# Install/uninstall fan-daemon systemd service
#
# Usage:
#   ./setup-fan-daemon.sh install      # Copy daemon to /usr/local/bin (production)
#   ./setup-fan-daemon.sh install-dev  # Symlink daemon (development)
#   ./setup-fan-daemon.sh uninstall
#   ./setup-fan-daemon.sh status
#
# Customizing the daemon:
#   View available options:
#     fan-daemon.py --help
#
#   To change settings after install, edit the service:
#     sudo systemctl edit fan-daemon --full
#
#   Then modify ExecStart, e.g.:
#     ExecStart=/usr/bin/python3 /usr/local/bin/fan-daemon.py --interval_seconds 10

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="fan-daemon"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DAEMON_INSTALL_PATH="/usr/local/bin/fan-daemon.py"

usage() {
    echo "Usage: $0 {install|install-dev|uninstall|status}"
    echo "  install      Copy daemon to /usr/local/bin (production)"
    echo "  install-dev  Symlink daemon (development - edits are live)"
    exit 1
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        echo "Error: must run as root"
        exit 1
    fi
}

install_daemon_copy() {
    rm -f "$DAEMON_INSTALL_PATH"
    install -m755 "$SCRIPT_DIR/fan-daemon.py" "$DAEMON_INSTALL_PATH"
    echo "  Copied $DAEMON_INSTALL_PATH"
}

install_daemon_symlink() {
    rm -f "$DAEMON_INSTALL_PATH"
    ln -s "$SCRIPT_DIR/fan-daemon.py" "$DAEMON_INSTALL_PATH"
    echo "  Symlinked $DAEMON_INSTALL_PATH -> $SCRIPT_DIR/fan-daemon.py"
}

install_service() {
    # Detect IPMI services to start after (they may reset fan speeds on boot)
    IPMI_SERVICES=$(systemctl list-unit-files --type=service --no-legend 2>/dev/null \
        | awk '{print $1}' \
        | grep -i 'ipmi' \
        | tr '\n' ' ')

    # Create service file
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Fan Control Daemon
After=network.target ${IPMI_SERVICES}

[Service]
Type=simple

# Add flags here. Run "fan-daemon.py --help" for options. Examples:
#   --interval_seconds 10              Poll every 10 seconds (default: 5)
#   --hysteresis_celsius 3             3°C temp hysteresis (default: 5)
#   --hysteresis_seconds 60            60s time hysteresis (default: 30)
#   --log-level DEBUG                  Verbose logging
#   --speeds gpu-zone1=50:30,70:100    Custom GPU curve for zone 1
#   --speeds gpu=70:80:5:60            With per-point hysteresis (5°C, 60s)
ExecStart=/usr/bin/python3 /usr/local/bin/fan-daemon.py

Restart=on-failure
RestartSec=5

# Fail-safe: set fans to full if service stops unexpectedly
ExecStopPost=/usr/bin/ipmitool raw 0x30 0x45 0x01 0x01
ExecStopPost=/usr/bin/ipmitool raw 0x30 0x70 0x66 0x01 0x00 0x64
ExecStopPost=/usr/bin/ipmitool raw 0x30 0x70 0x66 0x01 0x01 0x64

User=root

StandardOutput=journal
StandardError=journal
SyslogIdentifier=fan-daemon

[Install]
WantedBy=multi-user.target
EOF
    echo "  Installed $SERVICE_FILE"

    # Reload and enable
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    echo "  Enabled $SERVICE_NAME"

    # Start service
    systemctl start "$SERVICE_NAME"
    echo "  Started $SERVICE_NAME"

    echo ""
    echo "Done."
    echo "  Logs:   journalctl -u $SERVICE_NAME -f"
    echo "  Config: sudo systemctl edit $SERVICE_NAME --full"
}

do_install() {
    check_root
    echo "Installing fan-daemon..."
    install_daemon_copy
    install_service
}

do_install_dev() {
    check_root
    echo "Installing fan-daemon (dev mode)..."
    install_daemon_symlink
    install_service
}

do_uninstall() {
    check_root
    echo "Uninstalling fan-daemon..."

    # Stop and disable service
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        systemctl stop "$SERVICE_NAME"
        echo "  Stopped $SERVICE_NAME"
    fi

    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        systemctl disable "$SERVICE_NAME"
        echo "  Disabled $SERVICE_NAME"
    fi

    # Remove files
    if [ -f "$SERVICE_FILE" ]; then
        rm "$SERVICE_FILE"
        echo "  Removed $SERVICE_FILE"
    fi

    # Handle both regular files and symlinks
    if [ -e "$DAEMON_INSTALL_PATH" ] || [ -L "$DAEMON_INSTALL_PATH" ]; then
        rm "$DAEMON_INSTALL_PATH"
        echo "  Removed $DAEMON_INSTALL_PATH"
    fi

    systemctl daemon-reload

    # Set fans to full (safe state)
    echo "  Setting fans to full speed..."
    ipmitool raw 0x30 0x45 0x01 0x01 >/dev/null 2>&1 || true
    ipmitool raw 0x30 0x70 0x66 0x01 0x00 0x64 >/dev/null 2>&1 || true
    ipmitool raw 0x30 0x70 0x66 0x01 0x01 0x64 >/dev/null 2>&1 || true

    echo ""
    echo "Done. Fans set to full speed."
    echo "Use './fan-control.sh optimal' to return to auto control."
}

do_status() {
    echo "=== Service Status ==="
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        echo "Service: running"
    else
        echo "Service: stopped"
    fi

    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        echo "Enabled: yes"
    else
        echo "Enabled: no"
    fi

    echo ""
    echo "=== Fan Status ==="
    "$SCRIPT_DIR/fan-control.sh" status 2>/dev/null || true

    echo ""
    echo "=== Recent Logs ==="
    journalctl -u "$SERVICE_NAME" -n 5 --no-pager 2>/dev/null || echo "(no logs)"
}

case "${1:-}" in
    install)
        do_install
        ;;
    install-dev)
        do_install_dev
        ;;
    uninstall)
        do_uninstall
        ;;
    status)
        do_status
        ;;
    *)
        usage
        ;;
esac

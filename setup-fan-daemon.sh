#!/bin/bash
# Install/uninstall fan-daemon systemd service
#
# Usage:
#   ./install-fan-daemon.sh install
#   ./install-fan-daemon.sh uninstall
#   ./install-fan-daemon.sh status
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
    echo "Usage: $0 {install|uninstall|status}"
    exit 1
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        echo "Error: must run as root"
        exit 1
    fi
}

do_install() {
    check_root
    echo "Installing fan-daemon..."

    # Copy daemon script (remove first in case of symlink)
    rm -f "$DAEMON_INSTALL_PATH"
    cp "$SCRIPT_DIR/fan-daemon.py" "$DAEMON_INSTALL_PATH"
    chmod +x "$DAEMON_INSTALL_PATH"
    echo "  Installed $DAEMON_INSTALL_PATH"

    # Create service file
    cat > "$SERVICE_FILE" << 'EOF'
[Unit]
Description=Fan Control Daemon
After=network.target

[Service]
Type=simple

# Add flags here. Run "fan-daemon.py --help" for options. Examples:
#   --interval_seconds 10              Poll every 10 seconds (default: 5)
#   --hysteresis_celsius 3             3°C hysteresis (default: 5)
#   --log-level DEBUG                  Verbose logging
#   --speeds gpu-zone1=50:30,70:100    Custom GPU curve for zone 1
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

    if [ -f "$DAEMON_INSTALL_PATH" ]; then
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

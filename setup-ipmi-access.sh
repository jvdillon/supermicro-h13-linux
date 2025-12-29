#!/bin/bash
# Setup unprivileged IPMI access via udev rules
# Run with sudo, then log out and back in

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: Run with sudo"
    exit 1
fi

REAL_USER="${SUDO_USER:-$USER}"

# Create ipmi group if needed
if ! getent group ipmi >/dev/null; then
    groupadd ipmi
    echo "Created ipmi group"
fi

# Add user to ipmi group
if ! groups "$REAL_USER" | grep -qw ipmi; then
    usermod -aG ipmi "$REAL_USER"
    echo "Added $REAL_USER to ipmi group"
fi

# Load IPMI modules
modprobe ipmi_devintf 2>/dev/null || true
modprobe ipmi_si 2>/dev/null || true

# Ensure modules load at boot
for mod in ipmi_devintf ipmi_si; do
    grep -qxF "$mod" /etc/modules 2>/dev/null || echo "$mod" >> /etc/modules
done

# Create udev rule
cat > /etc/udev/rules.d/99-ipmi.rules << 'EOF'
KERNEL=="ipmi*", GROUP="ipmi", MODE="0660"
EOF

# Apply immediately
udevadm control --reload-rules
udevadm trigger --subsystem-match=ipmi

echo "Done. Log out and back in for group membership to take effect."

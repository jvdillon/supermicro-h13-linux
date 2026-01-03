# Supermicro H13 Linux Setup

Fan control and IPMI configuration for Supermicro H13-series motherboards
(H13SSL-N, H13SSW, etc.) running Linux.

## Contents

- `fan-daemon.py` - Automatic temperature-based fan control daemon
- `fan-control.sh` - Manual fan speed control
- `setup-fan-daemon.sh` - Install/uninstall the daemon as a systemd service
- `setup-ipmi-access.sh` - Configure unprivileged IPMI access
- `setup-ipmi-limits.sh` - Disable low-RPM alarms and set sensor thresholds
- `lshw.sh` - Hardware inventory script

## Quick Start

```bash
git clone https://github.com/jvdillon/supermicro-h13-linux.git
cd supermicro-h13-linux

# Setup unprivileged IPMI access (log out/in after)
sudo ./setup-ipmi-access.sh

# Disable low-RPM fan alarms
sudo ./setup-ipmi-limits.sh

# Install the automatic fan control daemon
sudo ./setup-fan-daemon.sh install

# Monitor the daemon
journalctl -u fan-daemon -f
```

## Fan Daemon

The `fan-daemon.py` script provides automatic temperature-based fan control.
It reads temperatures from CPU, GPU, RAM, HDD, and NVMe devices and adjusts
fan speeds using piecewise-constant curves with hysteresis.

### Features

- Per-device-type temperature mappings (CPU, GPU, RAM, HDD, NVMe)
- Two-zone control (zone 0: FAN1-4, zone 1: FANA-B)
- Hysteresis to prevent oscillation at threshold boundaries
- Fail-safe: any error sets fans to 100%
- GPU temps via nvidia-smi with IPMI fallback

### Installation

```bash
sudo ./setup-fan-daemon.sh install    # Install and start
sudo ./setup-fan-daemon.sh uninstall  # Stop and remove
sudo ./setup-fan-daemon.sh status     # Show status and logs
```

### Configuration

View options:

```bash
./fan-daemon.py --help
```

Override temperature mappings via command line:

```bash
# Format: DEVICE[N]-zone[M]=TEMP:SPEED[:HYST],TEMP:SPEED[:HYST],...
--mapping gpu-zone=50:20,70:50,85:100      # All GPUs, all zones
--mapping gpu0-zone1=60:30,80:100          # GPU 0, zone 1 only
--mapping hdd-zone=                         # Disable HDD control
```

To modify the installed service:

```bash
sudo systemctl edit fan-daemon --full
```

### Dependencies

```bash
sudo apt install ipmitool nvme-cli
# nvidia driver required for GPU temp monitoring
```

### Monitoring

```bash
journalctl -u fan-daemon -f
```

Output shows zone speeds and which device triggered the speed:

```
INFO: z0:GPU0=72C->40% z1:GPU0=72C->100% [cpu=45 gpu=72/70 ram=38 hdd=32 nvme=42]
```

## Manual Fan Control

The `fan-control.sh` script provides manual fan speed control via IPMI.

### Fan Zones

| Zone | Headers | Typical Use                     |
|------|---------|---------------------------------|
| 0    | FAN1-4  | CPU cooler, case intake/exhaust |
| 1    | FANA-B  | Auxiliary (GPU/peripheral)      |

### Usage

```bash
# Show current status
./fan-control.sh status

# Set zone to specific percentage (15-100%)
./fan-control.sh 0 50      # Zone 0 to 50%
./fan-control.sh 1 40      # Zone 1 to 40%
./fan-control.sh all 60    # Both zones to 60%

# Switch to BMC-controlled modes
./fan-control.sh optimal   # Optimal (auto)
./fan-control.sh standard  # Standard
./fan-control.sh heavyio   # Heavy IO
./fan-control.sh full      # Full speed
```

Notes:
- Minimum speed is 15%
- Manual percentages require "full" mode (set automatically)
- Use `optimal` to return to automatic BMC control

## IPMI Access Setup

By default, /dev/ipmi* devices require root. Run once:

```bash
sudo ./setup-ipmi-access.sh
# Log out and back in
```

This creates an `ipmi` group, loads kernel modules, and sets udev rules.

To add other users:

```bash
sudo usermod -aG ipmi USERNAME
```

## Sensor Thresholds

Factory fan thresholds trigger alarms at 420 RPM. For quiet operation:

```bash
sudo ./setup-ipmi-limits.sh
```

This disables low-RPM alarms. Run once after BMC reset.

## Hardware Inventory

```bash
./lshw.sh              # Full hardware details
./lshw.sh --noserial   # Hide serial numbers (for sharing)
```

## IPMI Raw Commands Reference

```bash
# Get current mode (00=standard, 01=full, 02=optimal, 04=heavyio)
ipmitool raw 0x30 0x45 0x00

# Set mode
ipmitool raw 0x30 0x45 0x01 0x00  # standard
ipmitool raw 0x30 0x45 0x01 0x01  # full (required for manual control)
ipmitool raw 0x30 0x45 0x01 0x02  # optimal
ipmitool raw 0x30 0x45 0x01 0x04  # heavyio

# Get zone duty cycle (returns hex percentage)
ipmitool raw 0x30 0x70 0x66 0x00 0x00  # Zone 0
ipmitool raw 0x30 0x70 0x66 0x00 0x01  # Zone 1

# Set zone duty cycle (0x00-0x64 = 0-100%)
ipmitool raw 0x30 0x70 0x66 0x01 0x00 0x32  # Zone 0 to 50%
ipmitool raw 0x30 0x70 0x66 0x01 0x01 0x28  # Zone 1 to 40%
```

## Tested Configuration

| Component   | Model                              |
|-------------|------------------------------------|
| Motherboard | Supermicro H13SSL-N                |
| CPU         | AMD EPYC 9555 (Turin/Zen 5)        |
| RAM         | 4x32GB DDR5-5600 ECC (SK Hynix)    |
| GPU         | 2x NVIDIA GeForce RTX 5090         |
| NVMe        | WD Black SN8100 4TB                |
| HDD         | Seagate Exos X18 12TB              |
| OS          | Ubuntu 24.04                       |

Should work on other H13-series boards with similar BMC firmware.

## Acknowledgments

IPMI raw commands derived from [smfc](https://github.com/petersulyok/smfc).

## License

Apache 2.0

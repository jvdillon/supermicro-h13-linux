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
sudo ./setup-fan-daemon.sh install      # Install and start (copies daemon)
sudo ./setup-fan-daemon.sh install-dev  # Install with symlink (for development)
sudo ./setup-fan-daemon.sh uninstall    # Stop and remove
sudo ./setup-fan-daemon.sh status       # Show status and logs
```

### Configuration

View options:

```bash
./fan-daemon.py --help
```

Override temperature mappings via command line:

```bash
# Format: DEVICE[N][-zone[M]]=TEMP:SPEED[:HYST_CEL[:HYST_SEC]],...
# HYST_CEL = temp hysteresis (C), HYST_SEC = time hysteresis (s)
# Empty value uses default: 70:80::60 = default temp hyst, 60s time hyst
--speeds gpu=50:20,70:50,85:100           # All GPUs, all zones
--speeds gpu0-zone1=60:30,80:100          # GPU 0, zone 1 only
--speeds gpu=70:80:5:30                   # 5C temp hyst, 30s time hyst
--speeds hdd=                             # Disable HDD control
```

To modify the installed service:

```bash
sudo systemctl edit fan-daemon --full
```

### Dependencies

```bash
sudo apt install ipmitool smartmontools nvme-cli
# nvidia driver required for GPU temp monitoring
```

### Monitoring

```bash
journalctl -u fan-daemon -f
```

Output shows zone speeds, per-device temps, and which device triggered each zone:

```
INFO: z0=40% z1=100%
      cpu0         45C  z0:15%
      ram0         38C  z0:15%
      ram1         35C  z0:15%
      nvme0        42C  z0:15%
      hdd0         32C  z0:15%
      gpu0         72C  z0:40%  z1:100%  <-- z0,z1
      gpu1         70C  z0:40%  z1:100%
      gpu_ipmi0    73C
      vrm_cpu0     42C
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
| HDD         | Seagate IronWolf Pro 12TB          |
| OS          | Ubuntu 24.04                       |

Should work on other H13-series boards with similar BMC firmware.

## Why Not smfc?

[smfc](https://github.com/petersulyok/smfc) is a mature fan control solution for Supermicro boards.
We wrote our own because of architectural differences:

| Feature | fan-daemon | smfc |
|---------|------------|------|
| Complexity | ~1050 lines, single file | ~3500 lines, multiple modules |
| Hardware abstraction | `Hardware` protocol for easy porting | Supermicro-specific |
| Zone overlap | One device can drive multiple zones | Each controller owns one zone, no overlap |
| Curves | Piecewise-constant with arbitrary breakpoints | Linear min/max interpolation |
| Config | CLI flags with sensible defaults | INI config file required |
| CPU temp | IPMI sensor | Kernel modules (coretemp/k10temp) |
| GPU temp | nvidia-smi with IPMI fallback | nvidia-smi only |
| Python | Typed (passes pyright strict) | Untyped |
| Dependencies | ipmitool (smartctl/nvme-cli optional) | Kernel modules required |

The key difference is zone overlap: our GPUs drive both case fans (zone 0, gentle curve)
and GPU-specific fans (zone 1, aggressive curve) simultaneously. smfc's architecture
requires one controller per zone with no overlap.

We use IPMI for CPU temps rather than kernel modules (coretemp/k10temp) because on server
boards the BMC is the authority—it has dedicated hardware with manufacturer-calibrated
sensors, exposes temps that kernel modules can't see (RAM, VRM, peripheral), and is the
same interface the BMC uses for its own fan control.

The code uses a `Hardware` protocol, so adding support for other motherboards is
straightforward—just implement the interface (`get_temps`, `set_zone_speed`, etc.).

## Acknowledgments

IPMI raw commands derived from [smfc](https://github.com/petersulyok/smfc).

## License

Apache 2.0

# Supermicro Motherboard Linux Setup

Fan control and IPMI configuration for server motherboards running Linux.
Tested on Supermicro [H13-series (H13SSL-N,
H13SSL-NT)](https://www.supermicro.com/en/products/motherboard/h13ssl-n) but
extensible to other boards and sensors via the `Hardware` and `Sensor`
protocols.

## Contents

- `fan-control.sh` - Manual fan speed control
- `fan-daemon.py` - Automatic temperature-based fan control daemon
- `visualize-temps.py` - Temperature log visualization and plotting
- `lshw.sh` - Hardware inventory script
- `setup-fan-daemon.sh` - Install/uninstall the daemon as a systemd service
- `setup-ipmi-access.sh` - Configure unprivileged IPMI access
- `setup-ipmi-limits.sh` - Disable low-RPM alarms and set sensor thresholds

## Features

**Manual control** - `fan-control.sh` provides simple per-zone fan speed
control via IPMI raw commands. Set exact percentages or switch between BMC
modes.

**Automatic daemon** - `fan-daemon.py` monitors temperatures and adjusts fan
speeds using piecewise-constant curves with configurable thresholds.

- **Multi-zone**: Different curves for case fans (zone 0) vs GPU fans (zone 1).
  One device can drive multiple zones with different curves.
- **Dual hysteresis**: Temperature hysteresis (deadband) prevents oscillation
  at thresholds. Time hysteresis (min hold) requires temps to stay low before
  dropping speed, handling bursty workloads.
- **Extensible sensors**: `Sensor` protocol for CPU (`k10temp`), GPU
  (`nvidia-smi`), NVMe (`nvme-cli`), HDD (`smartctl`), and IPMI (`ipmitool`).
  Add new sensors by implementing `get()`.
- **Modular hardware**: `Hardware` protocol abstracts board-specific IPMI
  commands. Port to other boards by implementing the interface.
- **Fail-safe**: Any error sets fans to 100%.

**Visualization** - `visualize-temps.py` scrapes `journalctl` logs, stores data in
npz, and generates temperature/fan speed plots for tuning curves.

## Quick Start

```bash
git clone https://github.com/jvdillon/supermicro-h13-linux.git
cd supermicro-h13-linux

# Setup unprivileged IPMI access (log out/in after)
sudo ./setup-ipmi-access.sh

# Disable low-RPM fan alarms
sudo ./setup-ipmi-limits.sh

# Install the automatic fan control daemon
sudo ./setup-fan-daemon.sh install  # Or install-dev

# Monitor the daemon
journalctl -u fan-daemon -f
```

## Fan Daemon

The `fan-daemon.py` script provides automatic temperature-based fan control. It
reads temperatures from CPU, GPU, RAM, HDD, and NVMe devices and adjusts fan
speeds using piecewise-constant curves with hysteresis.

### Features

- Per-device-type temperature mappings (CPU, GPU, RAM, HDD, NVMe)
- Two-zone control (zone 0: FAN1-4, zone 1: FANA-B)
- Hysteresis to prevent oscillation at threshold boundaries
- Fail-safe: any error sets fans to 100%
- GPU temps via `nvidia-smi` (IPMI available via `--ipmi-temps`)

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
# Format: DEVICE[N][-zone[M]]=TEMP:SPEED[:HYST_C[:HYST_S]],...
# HYST_C = temp hysteresis (C), HYST_S = time hysteresis (s)
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

Output shows zone speeds, per-device temps, and which device triggered each
zone:

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

For historical visualization and curve tuning, use `visualize-temps.py`:

```bash
./visualize-temps.py                    # Scrape logs, save to results/temps.npz, plot
./visualize-temps.py --since "2 hours"  # Limit time range
./visualize-temps.py --no-plot          # Just scrape and save data
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

The following IPMI raw commands are derived from
[smfc](https://github.com/petersulyok/smfc).

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

| Component   | Model                                  |
|-------------|----------------------------------------|
| Motherboard | Supermicro H13SSL-N                    |
| CPU         | AMD EPYC 9555 (Turin/Zen 5)            |
| RAM         | 4x32GB DDR5-5600 ECC (SK Hynix)        |
| GPU         | 2x NVIDIA GeForce RTX 5090             |
| NVMe        | WD Black SN8100 4TB                    |
| HDD         | Seagate IronWolf Pro 12TB              |
| PSU         | Seasonic PRIME PX-1600 (1600W Platinum)|
| Case        | Phanteks Enthoo Pro 2 Server Edition   |
| CPU Cooler  | ARCTIC Freezer 4U-SP5                  |
| Case Fans   | ARCTIC P14 Pro PST                     |
| GPU Fans    | ARCTIC P12 Slim PWM PST                |
| OS          | Ubuntu 24.04 LTS                       |

This project should work out-of-the-box on other H13-series boards with similar
BMC firmware. Other boards and sensors can be supported by implementing the
`Hardware` and `Sensor` protocols.

## Why not smfc?

[smfc](https://github.com/petersulyok/smfc) is a mature fan control solution
for Supermicro boards and an excellent project.

We opted to write our own because of the need for a fundamentally different
control loop architecture. The key difference is zone overlap: for example, our
GPUs drive both case fans (zone 0, gentle curve) and GPU-specific fans (zone 1,
aggressive curve) simultaneously. smfc's architecture requires one controller
per zone with no overlap. This difference makes sense because smfc appears to
be written more with HDD in mind than GPU, the latter having significant
case-wide temperature implications.

Summary of differences:

| Feature | fan-daemon | smfc |
|---------|------------|------|
| Device multizone | One device can drive multiple zones | Each controller owns one zone, no overlap |
| Zone arbitration | Max speed wins across all devices per zone | N/A (single controller per zone) |
| Curves | Piecewise-constant with arbitrary breakpoints | Linear min/max interpolation |
| Temp hysteresis | Deadband | Deadband (aka "Sensitivity") |
| Time hysteresis | Min hold: temp must stay low for N seconds (true hysteresis) | Delay after speed change |
| Config | CLI flags with sensible defaults | INI config file required |
| Complexity | ~1100 lines | ~3500 lines |
| Hardware abstraction | `Hardware` protocol for easy porting | Supermicro-specific |
| Sensor abstraction | `Sensor` protocol for easy extension | Hardcoded sensor types |
| Sensor merging | Logical devices (keys) from multiple physical sensors | Fixed sensor per device type |
| Python | Typed (passes pyright strict) | Untyped |

We support either using IPMI (BMC) for temps or device specific tools
(`coretemp/k10temp`, `nvidia-smi`, `smartctl`, `nvme-cli`, etc). Although BMC
is the authority on server boards (such as the H13)--it is slow and prone to
race conditions. By default we use the most specific sensor reader we can, but
offer an `ipmitool`-based sensor and a clean mechanism for merging sensors.

The `Hardware` protocol abstracts board-specific IPMI commands, so adding
support for other motherboards is straightforward--just implement the interface
(`get_temps`, `set_zone_speed`, etc.).

## Acknowledgments

IPMI raw commands derived from [smfc](https://github.com/petersulyok/smfc).

## License

Apache 2.0

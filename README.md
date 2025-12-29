# Supermicro H13 Linux Setup

Fan control and IPMI configuration for Supermicro H13-series motherboards
(H13SSL-N, H13SSW, etc.) running Linux.

## Quick Start

```bash
git clone https://github.com/jvdillon/supermicro-h13-linux.git
cd supermicro-h13-linux

# Setup unprivileged IPMI access
sudo ./setup-ipmi-access.sh

# Log out and back in, then control fans
./fancontrol.sh status
./fancontrol.sh 0 40    # Zone 0 to 40%
./fancontrol.sh optimal # Return to auto
```

## Fan Control

The `fancontrol.sh` script provides manual fan speed control via IPMI raw
commands.

### Fan Zones

| Zone | Fans    | Typical Use                    |
|------|---------|--------------------------------|
| 0    | FAN1-4  | CPU cooler, case intake/exhaust |
| 1    | FANA-B  | Auxiliary (GPU cooling)        |

### Usage

```bash
# Show current status
./fancontrol.sh status

# Set zone to specific percentage (15-100%)
./fancontrol.sh 0 50      # Zone 0 to 50%
./fancontrol.sh 1 40      # Zone 1 to 40%
./fancontrol.sh all 60    # Both zones to 60%

# Switch to BMC-controlled modes
./fancontrol.sh optimal   # Optimal (auto)
./fancontrol.sh standard  # Standard
./fancontrol.sh heavyio   # Heavy IO
./fancontrol.sh full      # Full speed

# Disable low-RPM alarms (run once after fresh BMC reset)
./fancontrol.sh init
```

### Notes

- Minimum speed is 15% (fans won't go lower)
- Setting a manual percentage switches to "full" mode first, then applies the
  duty cycle
- Use `optimal` to return to automatic BMC control
- Fan thresholds default to 420 RPM; `init` sets them to 0 to silence alarms
  when running quiet

## IPMI Access Setup

By default, `/dev/ipmi*` devices require root. The `setup-ipmi-access.sh`
script creates an `ipmi` group and udev rules for unprivileged access:

```bash
sudo ./setup-ipmi-access.sh
# Log out and back in
```

To add other users:
```bash
sudo usermod -aG ipmi USERNAME
```

## Hardware Reference

Tested configuration:

| Component    | Model                                      |
|--------------|--------------------------------------------|
| Motherboard  | Supermicro H13SSL-N (AMD EPYC SP5)        |
| Case         | Phanteks Enthoo Pro 2 Server Edition      |
| CPU Cooler   | Arctic Freezer 4U-SP5                     |
| Case Fans    | Arctic P14 Pro PST (140mm)                |
| Aux Fans     | Arctic P12 Slim PWM PST (120mm, for GPU)  |

### Fan Wiring

```
Zone 0 (FAN1-4):
  FAN1 - CPU cooler
  FAN2 - Case intake
  FAN3 - Case intake
  FAN4 - Exhaust

Zone 1 (FANA-B):
  FANA - GPU cooling
  FANB - (unused)
```

## IPMI Raw Commands Reference

For those wanting to understand or modify the fan control:

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

# Set fan threshold (silence low-RPM alarms)
ipmitool sensor thresh FAN1 lower 0 0 0
```

## IPMIView (Optional)

Supermicro's Java-based IPMI management tool:

```bash
# Download from Supermicro support site
wget https://www.supermicro.com/.../IPMIView_*_bundleJRE_Linux_x64.tar.gz

tar xzf IPMIView_*_bundleJRE_Linux_x64.tar.gz
cd IPMIView_*

# Fix HiDPI scaling
export _JAVA_OPTIONS="-Dsun.java2d.uiScale.enabled=true -Dsun.java2d.uiScale=2.0"
./IPMIView20
```

## Tested On

- Supermicro H13SSL-N with AMD EPYC 9555 (Genoa/Turin)
- Ubuntu 24.04

Should work on other H13-series boards with similar BMC firmware.

## License

Apache 2.0

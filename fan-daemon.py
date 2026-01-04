#!/usr/bin/env python3
"""
Fan daemon for server motherboards using piecewise-constant temperature mappings.

Each device type (CPU, GPU, RAM, HDD, NVMe) has its own temp->speed mapping.
Fan speed = max(mapping(device_temp) for all devices).
Logs which device triggered the speed change.

Fail-safe: Any error -> full speed (100%)

Run with --help for configuration options.

Monitor logs:
    journalctl -u fan-daemon -f

Dependencies:
    sudo apt install ipmitool smartmontools nvme-cli nvidia-open
    # ipmitool      - IPMI fan control + RAM/VRM temp monitoring
    # smartmontools - HDD temperature monitoring via smartctl (optional)
    # nvme-cli      - NVMe temperature monitoring (optional)
    # nvidia-open   - nvidia-smi (+driver) for GPU temp monitoring (optional)
    # k10temp       - AMD CPU temp via hwmon (kernel module, usually built-in)
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import re
import signal
import sys
import time
from typing import Protocol, cast, final

import sensors
from sensors import run_cmd

log = logging.getLogger("fan-daemon")

# (threshold_temp, fan_speed_percent, hysteresis_celsius)
# hysteresis=None means use global default
MappingPoint = tuple[float, float, float | None]
MappingKey = tuple[str, int, int]  # (device_type, device_idx, zone); -1 means "all"


class Hardware(Protocol):
    """Hardware interface protocol."""

    def initialize(self) -> bool: ...
    def get_temps(self) -> dict[str, tuple[int, ...] | None] | None: ...
    def get_zones(self) -> tuple[int, ...]: ...
    def set_zone_speed(self, zone: int, percent: int) -> bool: ...
    def set_fail_safe(self) -> bool: ...


@final
class SupermicroH13:
    """Hardware implementation for Supermicro H13-series motherboards."""

    @dataclasses.dataclass(slots=True, kw_only=True)
    class Config:
        """Hardware configuration."""

        zones: tuple[int, ...] = (0, 1)
        ipmi_write_delay_seconds: float = 2.0
        ipmi_ready_timeout_seconds: float = 120.0
        ipmi_ready_retry_seconds: float = 5.0
        ipmi_temps: bool = False  # Use ipmitool for RAM/VRM temps

        # IPMI sensor name -> result_key
        # Keys that duplicate other sensors get _ipmi suffix
        ipmi_sensors: dict[str, str] = dataclasses.field(
            default_factory=lambda: {
                "CPU Temp": "cpu_ipmi",
                "DIMMA~F Temp": "ram",
                "DIMMG~L Temp": "ram",
                "GPU1 Temp": "gpu_ipmi",
                "GPU2 Temp": "gpu_ipmi",
                "GPU3 Temp": "gpu_ipmi",
                "GPU4 Temp": "gpu_ipmi",
                "GPU5 Temp": "gpu_ipmi",
                "GPU6 Temp": "gpu_ipmi",
                "GPU7 Temp": "gpu_ipmi",
                "GPU8 Temp": "gpu_ipmi",
                "SOC_VRM Temp": "vrm_soc",
                "CPU_VRM0 Temp": "vrm_cpu",
                "CPU_VRM1 Temp": "vrm_cpu",
                "VDDIO_VRM Temp": "vrm_vddio",
                "System Temp": "system",
                "Peripheral Temp": "peripheral",
            }
        )

        def setup(self) -> SupermicroH13:
            """Build SupermicroH13 from this config."""
            return SupermicroH13(self)

        @classmethod
        def add_args(cls, argparser: argparse.ArgumentParser) -> None:
            """Add hardware arguments to parser."""
            _ = argparser.add_argument(
                "--ipmi-temps",
                action="store_true",
                help="Read RAM/VRM temps via ipmitool (slower, more BMC traffic).",
            )

        @classmethod
        def from_args(
            cls,
            argparser: argparse.ArgumentParser,
            args: argparse.Namespace,
        ) -> SupermicroH13.Config:
            """Create Config from parsed arguments."""
            del argparser  # unused
            return cls(ipmi_temps=cast(bool, args.ipmi_temps))

    def __init__(self, config: Config):
        self.config = config
        self._last_set_speeds: dict[int, int] = {}

        # Initialize sensors (detection happens in constructors)
        self._sensors: list[sensors.Sensor] = [
            sensors.K10Temp(),
            sensors.Nvidiasmi(),
            sensors.Smartctl(),
            sensors.Nvmecli(),
        ]
        if config.ipmi_temps:
            self._sensors.append(sensors.Ipmitool(config.ipmi_sensors))

    def initialize(self) -> bool:
        """Initialize hardware for manual fan control. Sets BMC to full mode.

        Retries up to ipmi_ready_timeout_seconds if BMC is not ready.
        """
        deadline = time.time() + self.config.ipmi_ready_timeout_seconds
        while True:
            if self._set_full_mode():
                return True
            if time.time() >= deadline:
                log.error(
                    "BMC not ready after %.0fs", self.config.ipmi_ready_timeout_seconds
                )
                return False
            log.warning(
                "BMC not ready, retrying in %.0fs...",
                self.config.ipmi_ready_retry_seconds,
            )
            time.sleep(self.config.ipmi_ready_retry_seconds)

    def get_temps(self) -> dict[str, tuple[int, ...] | None] | None:
        """Get all temperatures. Returns None on critical failure."""
        result: dict[str, tuple[int, ...] | None] = {}

        for sensor in self._sensors:
            sensor_result = sensor.get()
            for key, temps in sensor_result.items():
                if temps is not None:
                    # Convert floats to ints
                    result[key] = tuple(int(t) for t in temps)
                elif key not in result:
                    result[key] = None

        # CPU and GPU are required
        if not result.get("cpu"):
            log.error("Failed to read CPU temp")
            return None
        if not result.get("gpu"):
            log.error("Failed to read GPU temp")
            return None

        return result

    def get_zones(self) -> tuple[int, ...]:
        """Get available fan zones."""
        return self.config.zones

    def set_zone_speed(self, zone: int, percent: int) -> bool:
        """Set fan zone speed. Skips if already at requested speed."""
        if self._last_set_speeds.get(zone) == percent:
            return True
        log.debug("IPMI set zone %d to %d%% (0x%02x)", zone, percent, percent)
        out = run_cmd(
            [
                "ipmitool",
                "raw",
                "0x30",
                "0x70",
                "0x66",
                "0x01",
                f"0x{zone:02x}",
                f"0x{percent:02x}",
            ]
        )
        if out is None:
            log.error("Failed to set zone %d to %d%%", zone, percent)
            return False
        self._last_set_speeds[zone] = percent
        # Let BMC settle after command
        time.sleep(self.config.ipmi_write_delay_seconds)
        # Verify read-back in debug mode
        if log.isEnabledFor(logging.DEBUG):
            actual = self._get_zone_speed(zone)
            if actual is not None and actual != percent:
                log.warning(
                    "Zone %d: set %d%% but BMC reports %d%%", zone, percent, actual
                )
        return True

    def _get_zone_speed(self, zone: int) -> int | None:
        """Get current fan zone speed from BMC."""
        out = run_cmd(
            ["ipmitool", "raw", "0x30", "0x70", "0x66", "0x00", f"0x{zone:02x}"]
        )
        if not out:
            return None
        try:
            return int(out.strip(), 16)
        except ValueError:
            return None

    def set_fail_safe(self) -> bool:
        """Set BMC to full mode and clear speed cache. Retries until success."""
        self._last_set_speeds.clear()
        deadline = time.time() + self.config.ipmi_ready_timeout_seconds
        while True:
            if self._set_full_mode():
                return True
            if time.time() >= deadline:
                log.error(
                    "Failed to set fail-safe after %.0fs",
                    self.config.ipmi_ready_timeout_seconds,
                )
                return False
            log.warning(
                "Fail-safe failed, retrying in %.0fs...",
                self.config.ipmi_ready_retry_seconds,
            )
            time.sleep(self.config.ipmi_ready_retry_seconds)

    def _set_full_mode(self) -> bool:
        """Ensure BMC is in full/manual fan mode."""
        out = run_cmd(["ipmitool", "raw", "0x30", "0x45", "0x00"])
        if out is None or out.strip() != "01":
            if run_cmd(["ipmitool", "raw", "0x30", "0x45", "0x01", "0x01"]) is None:
                log.error("Failed to set full fan mode")
                return False
            time.sleep(self.config.ipmi_write_delay_seconds)
        return True


@final
class FanSpeed:
    """Temperature-to-fan-speed mapping lookup with precedence."""

    @dataclasses.dataclass(slots=True, kw_only=True)
    class Config:
        """Handles --speeds and --hysteresis_celsius flags."""

        hysteresis_celsius: float = 5.0

        # (temp, speed, hysteresis) - hysteresis=None means use global default
        # Throttle temps: CPU 100°C, GPU 90°C, RAM 85°C, HDD 70°C, NVMe 85°C
        # Generally we set 100% at 85% of throttle.
        speeds: dict[MappingKey, tuple[MappingPoint, ...] | None] = dataclasses.field(
            default_factory=lambda: {
                # CPU: AMD EPYC 9555 - throttle 100°C (85%: 85°C)
                ("cpu", -1, 0): (
                    (0, 15, None),
                    (60, 25, None),
                    (70, 40, None),
                    (80, 70, None),
                    (85, 100, None),
                ),
                # RAM: DDR5 SK Hynix - max 85°C (85%: 72°C)
                # Requires --ipmi-temps flag to enable RAM temp monitoring
                # ("ram", -1, 0): (
                #     (0, 15, None),
                #     (50, 25, None),
                #     (60, 40, None),
                #     (68, 70, None),
                #     (72, 100, None),
                # ),
                # NVMe: WD Black SN8100 - max 85°C (85%: 72°C)
                ("nvme", -1, 0): (
                    (0, 15, None),
                    (50, 25, None),
                    (58, 40, None),
                    (65, 70, None),
                    (72, 100, None),
                ),
                # HDD: Seagate IronWolf Pro - max 70°C (85%: 60°C)
                ("hdd", -1, 0): (
                    (0, 15, None),
                    (42, 25, None),
                    (48, 40, None),
                    (54, 70, None),
                    (60, 100, None),
                ),
                # GPU: NVIDIA RTX 5090 - throttle 90°C (85%: 77°C)
                # Zone 0 (case fans): ramp earlier to help GPU cooling
                ("gpu", -1, 0): (
                    (0, 15, None),
                    (40, 25, None),
                    (50, 40, None),
                    (60, 60, None),
                    (70, 80, None),
                    (80, 100, None),
                ),
                # Zone 1 (GPU fans): aggressive - 100% above 50°C
                ("gpu", -1, 1): (
                    (0, 15, None),
                    (35, 40, None),
                    (45, 80, None),
                    (50, 100, None),
                ),
            }
        )

        def setup(self) -> FanSpeed:
            """Build FanSpeed from this config."""
            return FanSpeed(self)

        @classmethod
        def add_args(cls, argparser: argparse.ArgumentParser) -> None:
            """Add mapping arguments to parser."""
            _ = argparser.add_argument(
                "--hysteresis_celsius",
                type=float,
                default=5.0,
                help="Hysteresis (C) for falling temps.",
            )
            _ = argparser.add_argument(
                "--speeds",
                action="append",
                metavar="SPEC",
                help="Mapping spec. Repeatable.",
            )

        @classmethod
        def from_args(
            cls,
            argparser: argparse.ArgumentParser,
            args: argparse.Namespace,
        ) -> FanSpeed.Config:
            """Create Config from parsed arguments."""
            config = cls(hysteresis_celsius=cast(float, args.hysteresis_celsius))
            for spec in cast(list[str], args.speeds or []):
                try:
                    key, speeds = cls._parse_speeds(spec)
                    config.speeds[key] = speeds
                except ValueError as e:
                    argparser.error(str(e))
            return config

        @classmethod
        def _parse_speeds(
            cls,
            spec: str,
        ) -> tuple[MappingKey, tuple[MappingPoint, ...] | None]:
            """Parse 'gpu0-zone1=40:15,80:100' into ((device, idx, zone), mapping).

            Zone is optional: 'gpu=...' means all zones.
            Empty value (e.g. 'gpu=') returns None mapping (disables device).
            """
            if "=" not in spec:
                raise ValueError("Invalid mapping spec (missing '='): %s" % spec)
            key_part, value_part = spec.split("=", 1)

            # Parse key
            m = re.match(
                r"^([a-z][a-z_]*)(\d+)?(?:-zone(\d+)?)?$",
                key_part.strip().lower(),
            )
            if not m:
                raise ValueError("Invalid mapping key format: %s" % key_part)
            key: MappingKey = (
                m.group(1),
                int(m.group(2)) if m.group(2) else -1,
                int(m.group(3)) if m.group(3) else -1,
            )

            # Parse value
            value_part = value_part.strip()
            if not value_part:
                return key, None
            points: list[MappingPoint] = []
            for part in value_part.split(","):
                part = part.strip()
                if not part:
                    continue
                pieces = part.split(":")
                if len(pieces) < 2 or len(pieces) > 3:
                    raise ValueError(
                        "Invalid point format: %s (expected temp:speed[:hyst])" % part
                    )
                temp, speed = float(pieces[0]), float(pieces[1])
                hyst: float | None = float(pieces[2]) if len(pieces) == 3 else None
                if not 0 <= speed <= 100:
                    raise ValueError("Speed must be 0-100, got %s" % speed)
                if hyst is not None and hyst < 0:
                    raise ValueError("Hysteresis must be >= 0, got %s" % hyst)
                points.append((temp, speed, hyst))
            if len(points) < 2:
                raise ValueError("Mapping must have at least 2 points")
            points.sort(key=lambda pp: pp[0])
            return key, tuple(points)

    def __init__(self, config: Config):
        self.config = config

    def get(
        self,
        device_type: str,
        device_idx: int,
        zone: int,
    ) -> tuple[tuple[MappingPoint, ...], MappingKey] | None:
        """Look up mapping with precedence: deviceN-zoneM > deviceN-zone > device-zoneM > device-zone.

        Returns (mapping, matched_key) or None. matched_key shows which key was used,
        including whether zone was -1 (wildcard).
        """
        for k in [
            (device_type, device_idx, zone),
            (device_type, device_idx, -1),
            (device_type, -1, zone),
            (device_type, -1, -1),
        ]:
            if k in self.config.speeds:
                mapping = self.config.speeds[k]
                if mapping is not None:
                    return mapping, k
        return None

    def lookup(
        self,
        temp: float,
        mapping: tuple[MappingPoint, ...],
        active_threshold: float | None = None,
    ) -> tuple[float, float]:
        """Piecewise constant lookup with hysteresis.

        Returns (speed, new_active_threshold).

        When temp is falling (below active_threshold), stays at current threshold
        until temp drops below (threshold - hysteresis).
        """
        # Find normal threshold (highest t where temp >= t)
        normal_idx = -1
        for i, (t, _, _) in enumerate(mapping):
            if temp >= t:
                normal_idx = i

        if normal_idx < 0:
            # Below all thresholds, use first speed
            return mapping[0][1], mapping[0][0]

        normal_thresh = mapping[normal_idx][0]
        normal_speed = mapping[normal_idx][1]

        if active_threshold is None:
            return normal_speed, normal_thresh

        # Find index of active threshold
        active_idx = -1
        for i, (t, _, _) in enumerate(mapping):
            if t == active_threshold:
                active_idx = i
                break

        if active_idx < 0 or normal_idx >= active_idx:
            # Rising or same threshold, use normal
            return normal_speed, normal_thresh

        # Falling - check if we should drop
        active_t, active_s, active_h = mapping[active_idx]
        hyst = self.config.hysteresis_celsius if active_h is None else active_h
        if temp < active_t - hyst:
            # Drop to new threshold
            return normal_speed, normal_thresh
        else:
            # Stay at active threshold
            return active_s, active_t


@final
class FanDaemon:
    """Main fan control daemon."""

    @dataclasses.dataclass(slots=True, kw_only=True)
    class Config:
        """Daemon configuration."""

        interval_seconds: float = 5.0
        heartbeat_seconds: float = 0.0  # 0 = disabled

        @classmethod
        def add_args(cls, argparser: argparse.ArgumentParser) -> None:
            """Add daemon configuration arguments to parser."""
            del cls  # unused (slots=True prevents accessing defaults via cls)
            _ = argparser.add_argument(
                "--interval_seconds",
                type=float,
                default=5.0,
                help="Poll interval (seconds).",
            )
            _ = argparser.add_argument(
                "--heartbeat_seconds",
                type=float,
                default=0.0,
                help="Heartbeat interval (seconds). 0=disabled.",
            )

        @classmethod
        def from_args(
            cls,
            argparser: argparse.ArgumentParser,
            args: argparse.Namespace,
        ) -> FanDaemon.Config:
            """Create Config from parsed arguments."""
            interval_seconds = cast(float, args.interval_seconds)
            if interval_seconds <= 0:
                argparser.error("--interval_seconds must be > 0")

            return cls(
                interval_seconds=interval_seconds,
                heartbeat_seconds=cast(float, args.heartbeat_seconds),
            )

        def setup(self, hardware: Hardware, speed: FanSpeed) -> FanDaemon:
            """Build FanDaemon from this config."""
            return FanDaemon(self, hardware, speed)

    def __init__(self, config: Config, hardware: Hardware, speed: FanSpeed):
        self.config = config
        self.hardware = hardware
        self.speed = speed
        self.running = False
        self.active_thresholds: dict[tuple[str, int, int], float] = {}
        self.last_logged_speeds: dict[int, int] = {}
        self.last_heartbeat = 0.0

    def run(self) -> None:
        """Main daemon loop."""
        _ = signal.signal(signal.SIGTERM, self.shutdown)
        _ = signal.signal(signal.SIGINT, self.shutdown)

        log.info("Starting: zones=%s", list(self.hardware.get_zones()))

        if not self.hardware.initialize():
            log.error("Failed to initialize hardware")
            sys.exit(1)

        # Verify all speed mappings have corresponding sensors
        temps = self.hardware.get_temps()
        if temps is None:
            log.error("Failed to read temps during startup")
            sys.exit(1)
        missing = self._check_mappings(temps)
        if missing:
            log.error(
                "Speed mappings reference missing sensors: %s", ", ".join(missing)
            )
            log.error("Available sensors: %s", ", ".join(sorted(temps.keys())))
            sys.exit(1)

        self.running = True
        while self.running:
            try:
                self.control_loop()
            except Exception:
                log.exception("Control loop error")
                _ = self.hardware.set_fail_safe()

            time.sleep(self.config.interval_seconds)

        _ = self.hardware.set_fail_safe()

    def control_loop(self) -> None:
        """Main control loop iteration."""
        temps = self.hardware.get_temps()
        if temps is None:
            log.error("Failed to read temps, setting fail-safe")
            _ = self.hardware.set_fail_safe()
            self.active_thresholds.clear()
            self.last_logged_speeds.clear()
            return

        zone_speeds = self._compute_zone_speeds(temps)

        for zone, (speed, _trigger, _trigger_temp) in zone_speeds.items():
            if not self.hardware.set_zone_speed(zone, speed):
                _ = self.hardware.set_fail_safe()
                self.active_thresholds.clear()
                self.last_logged_speeds.clear()
                return

        # Check if any speeds changed
        current_speeds = {z: spd for z, (spd, _, _) in zone_speeds.items()}
        speeds_changed = current_speeds != self.last_logged_speeds

        # Check if heartbeat is due (0 = disabled)
        now = time.time()
        hb = self.config.heartbeat_seconds
        heartbeat_due = hb > 0 and (now - self.last_heartbeat) >= hb

        status = self._format_status(zone_speeds, temps)

        if speeds_changed:
            log.info(status)
            self.last_logged_speeds = current_speeds
            self.last_heartbeat = now
        elif heartbeat_due:
            # Append (heartbeat) to first line
            lines = status.split("\n", 1)
            lines[0] += " (heartbeat)"
            log.info("\n".join(lines))
            self.last_heartbeat = now
        else:
            log.debug(status)

    def shutdown(
        self,
        signum: int | None = None,
        _frame: object = None,
    ) -> None:
        """Clean shutdown - set fans to fail-safe."""
        log.info("Shutting down (signal %d)", signum or 0)
        self.running = False
        _ = self.hardware.set_fail_safe()
        sys.exit(0)

    def _format_status(
        self,
        zone_speeds: dict[int, tuple[int, str, int]],
        temps: dict[str, tuple[int, ...] | None],
    ) -> str:
        """Format multiline status for logging."""
        lines: list[str] = []
        zones = sorted(self.hardware.get_zones())

        # First line: zone speeds
        zone_parts = [f"z{z}={spd}%" for z, (spd, _, _) in sorted(zone_speeds.items())]
        lines.append(" ".join(zone_parts))

        # Find winners for each zone (e.g., {0: "RAM0", 1: "GPU0"})
        winners: dict[int, str] = {z: trig for z, (_, trig, _) in zone_speeds.items()}

        # Collect all device names for alignment
        all_names: list[str] = []
        for name, values in temps.items():
            for idx in range(len(values or ())):
                all_names.append(f"{name}{idx}")
        max_name_len = max((len(n) for n in all_names), default=10)

        # Partition devices: those with curves first, then informational
        devices_with_curves: list[tuple[str, int, int]] = []
        devices_without_curves: list[tuple[str, int, int]] = []
        for name, values in temps.items():
            for idx, temp in enumerate(values or ()):
                has_curve = any(self.speed.get(name, idx, z) is not None for z in zones)
                if has_curve:
                    devices_with_curves.append((name, idx, temp))
                else:
                    devices_without_curves.append((name, idx, temp))

        # Format each device (curves first, then informational)
        for name, idx, temp in devices_with_curves + devices_without_curves:
            device_name = f"{name}{idx}"
            device_tag = f"{name.upper()}{idx}"

            # Get zone speeds from curves, tracking if wildcard
            zone_results: list[
                tuple[int, bool] | None
            ] = []  # (speed, is_wildcard) per zone
            for zone in zones:
                result = self.speed.get(name, idx, zone)
                if result is not None:
                    mapping, matched_key = result
                    is_wildcard = matched_key[2] == -1
                    active_thresh = self.active_thresholds.get((name, idx, zone))
                    spd, _ = self.speed.lookup(temp, mapping, active_thresh)
                    zone_results.append((int(spd), is_wildcard))
                else:
                    zone_results.append(None)

            # Build zone string - use z* if all results are from same wildcard curve
            if all(r is not None and r[1] for r in zone_results):
                # All zones use wildcard curve with same speed
                speeds = [r[0] for r in zone_results if r is not None]
                if len(set(speeds)) == 1:
                    zone_str = f"z*:{speeds[0]}%"
                else:
                    # Different speeds even with wildcard (shouldn't happen normally)
                    zone_str = "  ".join(
                        f"z{z}:{r[0]}%" if r else ""
                        for z, r in zip(zones, zone_results)
                    )
            else:
                # Mix of specific zones or no wildcard
                parts: list[str] = []
                for z, r in zip(zones, zone_results):
                    if r is not None:
                        parts.append(f"z{z}:{r[0]}%")
                zone_str = "  ".join(parts)

            # Check if this device is a winner for any zone
            winner_zones = [z for z, w in winners.items() if w == device_tag]
            winner_marker = ""
            if winner_zones:
                winner_marker = "  <-- " + ",".join(f"z{z}" for z in winner_zones)

            # Format the line with alignment
            line = f"      {device_name:<{max_name_len}}  {temp:>3}C"
            if zone_str:
                line += f"  {zone_str}"
            line += winner_marker
            lines.append(line)

        return "\n".join(lines)

    def _check_mappings(
        self,
        temps: dict[str, tuple[int, ...] | None],
    ) -> list[str]:
        """Check that all speed mapping device types have sensors.

        Returns list of missing device types.
        """
        # Get all device types from speed mappings
        mapping_types: set[str] = set()
        for (device_type, _, _), mapping in self.speed.config.speeds.items():
            if mapping is not None:  # Skip disabled mappings
                mapping_types.add(device_type)

        # Check each mapping type exists in temps
        missing: list[str] = []
        for device_type in sorted(mapping_types):
            if device_type not in temps:
                missing.append(device_type)
        return missing

    def _compute_zone_speeds(
        self,
        temps: dict[str, tuple[int, ...] | None],
    ) -> dict[int, tuple[int, str, int]]:
        """Compute fan speed per zone. Returns {zone: (speed, trigger, temp)}."""
        results: dict[int, tuple[int, str, int]] = {}
        for zone in self.hardware.get_zones():
            # (speed, trigger_name, temp, dev_name, dev_idx, new_thresh)
            candidates: list[tuple[float, str, int, str, int, float]] = []
            for name, values in temps.items():
                for idx, temp in enumerate(values or ()):
                    if (result := self.speed.get(name, idx, zone)) is not None:
                        mapping, _ = result
                        key = (name, idx, zone)
                        active_thresh = self.active_thresholds.get(key)
                        spd, new_thresh = self.speed.lookup(
                            temp, mapping, active_thresh
                        )
                        candidates.append(
                            (spd, f"{name.upper()}{idx}", temp, name, idx, new_thresh)
                        )
            if candidates:
                spd, trigger, temp, dev_name, dev_idx, new_thresh = max(
                    candidates, key=lambda x: x[0]
                )
                self.active_thresholds[(dev_name, dev_idx, zone)] = new_thresh
                results[zone] = (int(spd), trigger, temp)
            else:
                # No mappings for this zone - fail-safe to 100%
                results[zone] = (100, "none", 0)
        return results


def main() -> None:
    argparser = argparse.ArgumentParser(
        description="Fan daemon for server motherboards",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Mapping format: DEVICE[N][-zone[M]]=TEMP:SPEED[:HYST],TEMP:SPEED[:HYST],...
  HYST is optional per-point hysteresis (default: --hysteresis_celsius value).
  Hysteresis prevents oscillation: fans stay high until temp drops HYST below threshold.

  Examples:
    --speeds gpu=50:15,85:100            All GPUs, all zones
    --speeds gpu-zone0=50:15,85:100      All GPUs, zone 0 only
    --speeds gpu0-zone1=60:20,85:100     GPU #0, zone 1 only
    --speeds gpu=50:15:3,70:50:5         Custom hysteresis per point
    --speeds hdd=                        Disable HDD mappings

  Precedence (most specific wins):
    gpu0-zone0 > gpu0-zone > gpu-zone0 > gpu > default
""",
    )
    FanSpeed.Config.add_args(argparser)
    SupermicroH13.Config.add_args(argparser)
    FanDaemon.Config.add_args(argparser)
    _ = argparser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level.",
    )
    args = argparser.parse_args()

    # Configure logging first, before any setup functions run
    level = cast(int, getattr(logging, cast(str, args.log_level)))
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
    )

    daemon = FanDaemon.Config.from_args(
        argparser,
        args,
    ).setup(
        hardware=SupermicroH13.Config.from_args(
            argparser,
            args,
        ).setup(),
        speed=FanSpeed.Config.from_args(
            argparser,
            args,
        ).setup(),
    )
    daemon.run()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Fan daemon for server motherboards using piecewise-constant temperature mappings.

Each device type (CPU, GPU, HDD, NVMe) has its own temp→speed mapping.
Fan speed = max(mapping(device_temp) for all devices).
Logs which device triggered the speed change.

Fail-safe: Any error -> full speed (100%)

Run with --help for configuration options.

Monitor logs:
    journalctl -u fan-daemon -f

Dependencies:
    sudo apt install ipmitool nvme-cli nvidia-open
    # ipmitool     - IPMI fan control and CPU/RAM temp monitoring
    # nvme-cli     - NVMe temperature monitoring (optional)
    # nvidia-open  - GPU temperature monitoring via nvidia-smi (optional)
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import pathlib
import re
import signal
import subprocess
import sys
import time
from typing import Protocol, cast

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
log = logging.getLogger("fan-daemon")

# (threshold_temp, fan_speed_percent, hysteresis_celsius)
# hysteresis=0 means use global default
MappingPoint = tuple[float, float, float]
DeviceCelsiusToFanZonePercent = tuple[MappingPoint, ...]
MappingKey = tuple[str, int, int]  # (device_type, device_idx, zone); -1 means "all"


class Mappings:
    """Temperature-to-fan-speed mapping lookup with precedence."""

    mappings: dict[MappingKey, DeviceCelsiusToFanZonePercent | None]

    def __init__(
        self,
        overrides: dict[MappingKey, DeviceCelsiusToFanZonePercent | None] | None = None,
    ):
        # (temp, speed, hysteresis) - hysteresis=0 means use global default
        # Throttle temps: CPU 100°C, GPU 90°C, RAM 85°C, HDD 60°C, NVMe 85°C
        # Generally we set 100% at 85% of throttle.
        self.mappings = {
            # CPU: AMD EPYC 9555 - throttle 100°C
            ("cpu", -1, 0): (
                (40, 15, 0),
                (60, 30, 0),
                (75, 60, 0),
                (85, 100, 0),
            ),
            # GPU: NVIDIA RTX 5090 - throttle 90°C
            ("gpu", -1, 0): (
                (40, 15, 0),
                (50, 25, 0),
                (60, 30, 0),
                (70, 40, 0),
                (80, 60, 0),
                (87, 100, 0),
            ),
            ("gpu", -1, 1): (  # Essentially the GPU zone.
                (40, 30, 0),
                (50, 50, 0),
                (60, 70, 0),
                (70, 100, 0),
            ),
            # RAM: DDR5 SK Hynix - max 85°C
            ("ram", -1, 0): (
                (40, 15, 0),
                (60, 25, 0),
                (70, 50, 0),
                (80, 100, 0),
            ),
            # HDD: Seagate Exos X18 - max 60°C
            ("hdd", -1, 0): (
                (25, 15, 0),
                (40, 25, 0),
                (45, 50, 0),
                (50, 100, 0),
            ),
            # NVMe: WD Black SN8100 - max 85°C
            ("nvme", -1, 0): (
                (35, 15, 0),
                (50, 30, 0),
                (60, 60, 0),
                (70, 100, 0),
            ),
        }
        if overrides:
            self.mappings.update(overrides)

    def get(
        self,
        device_type: str,
        device_idx: int,
        zone: int,
    ) -> DeviceCelsiusToFanZonePercent | None:
        """Look up mapping with precedence: deviceN-zoneM > deviceN-zone > device-zoneM > device-zone."""
        for k in [
            (device_type, device_idx, zone),
            (device_type, device_idx, -1),
            (device_type, -1, zone),
            (device_type, -1, -1),
        ]:
            if k in self.mappings:
                return self.mappings[k]
        return None

    @classmethod
    def parse(cls, s: str) -> DeviceCelsiusToFanZonePercent | None:
        """Parse "temp:speed[:hyst],..." into mapping. Empty string -> None (disabled)."""
        s = s.strip()
        if not s:
            return None
        points: list[MappingPoint] = []
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            pieces = part.split(":")
            if len(pieces) < 2 or len(pieces) > 3:
                raise ValueError(
                    "Invalid point format: %s (expected temp:speed[:hyst])" % part
                )
            temp, speed = float(pieces[0]), float(pieces[1])
            hyst = float(pieces[2]) if len(pieces) == 3 else 0.0
            if not 0 <= speed <= 100:
                raise ValueError("Speed must be 0-100, got %s" % speed)
            if hyst < 0:
                raise ValueError("Hysteresis must be >= 0, got %s" % hyst)
            points.append((temp, speed, hyst))
        if len(points) < 2:
            raise ValueError("Mapping must have at least 2 points")
        points.sort(key=lambda p: p[0])
        return tuple(points)

    @classmethod
    def parse_spec(
        cls, spec: str
    ) -> tuple[MappingKey, DeviceCelsiusToFanZonePercent | None]:
        """Parse 'gpu0-zone1=40:15,80:100' into ((device, idx, zone), mapping)."""
        if "=" not in spec:
            raise ValueError("Invalid mapping spec (missing '='): %s" % spec)
        key_part, mapping_part = spec.split("=", 1)
        mapping = cls.parse(mapping_part)
        m = re.match(
            r"^(cpu|gpu|ram|hdd|nvme)(\d+)?-zone(\d+)?$",
            key_part.strip().lower(),
        )
        if not m:
            raise ValueError("Invalid mapping key format: %s" % key_part)
        return (
            m.group(1),
            int(m.group(2)) if m.group(2) else -1,
            int(m.group(3)) if m.group(3) else -1,
        ), mapping

    @classmethod
    def lookup(
        cls,
        temp: float,
        mapping: DeviceCelsiusToFanZonePercent,
        active_threshold: float | None = None,
        default_hysteresis: float = 5.0,
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
        hyst = active_h if active_h > 0 else default_hysteresis
        if temp < active_t - hyst:
            # Drop to new threshold
            return normal_speed, normal_thresh
        else:
            # Stay at active threshold
            return active_s, active_t


@dataclasses.dataclass(slots=True, kw_only=True)
class Temps:
    """Temperature readings in Celsius."""

    cpus_celsius: list[float]
    gpus_celsius: list[float]
    rams_celsius: list[float]
    hdds_celsius: list[float]
    nvmes_celsius: list[float]


def _run_cmd(cmd: list[str], timeout: float) -> str | None:
    """Run command with timeout. Returns stdout on success, None on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _detect_hdds() -> tuple[str, ...]:
    """Auto-detect HDDs (rotational disks)."""
    hdds: list[str] = []
    for block in pathlib.Path("/sys/block").iterdir():
        if not block.name.startswith("sd"):
            continue
        rotational = block / "queue" / "rotational"
        if rotational.exists() and rotational.read_text().strip() == "1":
            hdds.append(f"/dev/{block.name}")
    return tuple(sorted(hdds))


def _detect_nvmes() -> tuple[str, ...]:
    """Auto-detect NVMe devices."""
    nvmes: list[str] = []
    for dev in pathlib.Path("/dev").glob("nvme*n1"):
        nvmes.append(str(dev))
    return tuple(sorted(nvmes))


@dataclasses.dataclass(slots=True, kw_only=True)
class ZoneConfig:
    """Per-zone fan configuration."""

    min_speed_percent: int = 15
    max_speed_percent: int = 100
    speed_step_percent: int = 10


@dataclasses.dataclass(slots=True, kw_only=True)
class Config:
    """Daemon configuration."""

    mappings: Mappings = dataclasses.field(default_factory=Mappings)
    zones: dict[int, ZoneConfig] = dataclasses.field(
        default_factory=lambda: {
            0: ZoneConfig(),
            1: ZoneConfig(),
        }
    )
    hysteresis_celsius: float = 5.0
    interval_seconds: float = 5.0
    gpu_slots: int = 5
    ram_sensors: tuple[str, ...] = ("DIMMA~F Temp", "DIMMG~L Temp")
    hdd_devices: tuple[str, ...] | None = None  # None = auto-detect
    nvme_devices: tuple[str, ...] | None = None  # None = auto-detect
    temp_min_valid_celsius: float = 0.0
    temp_max_valid_celsius: float = 120.0
    cmd_timeout_seconds: float = 5.0

    @classmethod
    def from_args(cls) -> Config:
        """Parse command-line arguments and return Config."""
        p = argparse.ArgumentParser(
            description="Fan daemon for server motherboards",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Mapping format: DEVICE[N]-zone[M]=TEMP:SPEED[:HYST],TEMP:SPEED[:HYST],...
  HYST is optional per-point hysteresis (default: --hysteresis value).
  Hysteresis prevents oscillation: fans stay high until temp drops HYST below threshold.

  Examples:
    --mapping gpu-zone=50:15,85:100       All GPUs, all zones
    --mapping gpu-zone0=50:15,85:100      All GPUs, zone 0 only
    --mapping gpu0-zone1=60:20,85:100     GPU #0, zone 1 only
    --mapping gpu-zone=50:15:3,70:50:5    Custom hysteresis per point
    --mapping hdd-zone=                   Disable HDD mappings

  Precedence (most specific wins):
    gpu0-zone0 > gpu0-zone > gpu-zone0 > gpu-zone > default
""",
        )
        dz = ZoneConfig()
        _ = p.add_argument(
            "--mapping",
            action="append",
            metavar="SPEC",
            help="Mapping spec. Repeatable.",
        )
        _ = p.add_argument(
            "--min-speed",
            type=int,
            default=dz.min_speed_percent,
            help="Min fan speed %%.",
        )
        _ = p.add_argument(
            "--max-speed",
            type=int,
            default=dz.max_speed_percent,
            help="Max fan speed %%.",
        )
        _ = p.add_argument(
            "--speed-step",
            type=int,
            default=dz.speed_step_percent,
            help="Speed step %%.",
        )
        _ = p.add_argument(
            "--hysteresis",
            type=float,
            default=5.0,
            help="Default hysteresis (C) for falling temps.",
        )
        _ = p.add_argument(
            "--interval",
            type=float,
            default=5.0,
            help="Poll interval (seconds).",
        )
        _ = p.add_argument(
            "--gpu-slots",
            type=int,
            default=5,
            help="PCIe GPU slots for IPMI fallback.",
        )
        _ = p.add_argument(
            "--hdd-devices",
            type=str,
            default="",
            help="Comma-separated HDD paths.",
        )
        _ = p.add_argument(
            "--nvme-devices",
            type=str,
            default="",
            help="Comma-separated NVMe paths.",
        )
        args = p.parse_args()
        min_speed = cast(int, args.min_speed)
        max_speed = cast(int, args.max_speed)
        if min_speed >= max_speed:
            p.error("--min-speed must be less than --max-speed")
        user_mappings: dict[MappingKey, DeviceCelsiusToFanZonePercent | None] = {}
        for spec in cast(list[str], args.mapping or []):
            try:
                key, mapping = Mappings.parse_spec(spec)
                user_mappings[key] = mapping
            except ValueError as e:
                p.error(str(e))
        hdd_str = cast(str, args.hdd_devices)
        nvme_str = cast(str, args.nvme_devices)
        hdd: tuple[str, ...] | None = None  # auto-detect
        nvme: tuple[str, ...] | None = None  # auto-detect
        if hdd_str:
            hdd = tuple(x.strip() for x in hdd_str.split(",") if x.strip())
        if nvme_str:
            nvme = tuple(x.strip() for x in nvme_str.split(",") if x.strip())
        speed_step = cast(int, args.speed_step)
        hysteresis = cast(float, args.hysteresis)
        interval = cast(float, args.interval)
        gpu_slots = cast(int, args.gpu_slots)
        zones = {
            z: ZoneConfig(
                min_speed_percent=min_speed,
                max_speed_percent=max_speed,
                speed_step_percent=speed_step,
            )
            for z in (0, 1)
        }
        return cls(
            mappings=Mappings(user_mappings),
            zones=zones,
            hysteresis_celsius=hysteresis,
            interval_seconds=interval,
            gpu_slots=gpu_slots,
            hdd_devices=hdd,
            nvme_devices=nvme,
        )


class Hardware(Protocol):
    """Hardware interface protocol."""

    def get_cpu_temps(self) -> list[float] | None: ...
    def get_gpu_temps(self) -> list[float] | None: ...
    def get_ram_temps(self) -> list[float] | None: ...
    def get_hdd_temps(self) -> list[float] | None: ...
    def get_nvme_temps(self) -> list[float] | None: ...
    def set_zone_speed(self, zone: int, percent: int) -> bool: ...
    def set_full_speed(self) -> bool: ...
    def detect_gpus(self) -> int: ...


class Supermicro:
    """Hardware implementation for Supermicro motherboards."""

    config: Config
    current_speeds: dict[int, int | None]
    _nvidia_warned: bool

    _hdd_devices: tuple[str, ...]
    _nvme_devices: tuple[str, ...]

    def __init__(self, config: Config) -> None:
        self.config = config
        self.current_speeds = {z: None for z in config.zones}
        self._nvidia_warned = False
        self._hdd_devices = (
            config.hdd_devices if config.hdd_devices is not None else _detect_hdds()
        )
        self._nvme_devices = (
            config.nvme_devices if config.nvme_devices is not None else _detect_nvmes()
        )
        if self._hdd_devices:
            log.info("HDDs: %s", ", ".join(self._hdd_devices))
        if self._nvme_devices:
            log.info("NVMe: %s", ", ".join(self._nvme_devices))

    def get_cpu_temps(self) -> list[float] | None:
        """Get CPU temperatures via IPMI."""
        out = _run_cmd(
            ["ipmitool", "sdr", "get", "CPU Temp"],
            self.config.cmd_timeout_seconds,
        )
        if out is None:
            log.error("Failed to read CPU Temp")
            return None
        temp = self._parse_ipmi_temp(out)
        if temp is None:
            log.error("Failed to parse CPU Temp")
            return None
        return [temp]

    def get_gpu_temps(self) -> list[float] | None:
        """Get GPU temps via nvidia-smi, falling back to IPMI."""
        out = _run_cmd(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            self.config.cmd_timeout_seconds,
        )
        if out is not None:
            temps = self._parse_nvidia_temps(out)
            if temps is not None:
                return temps
        if not self._nvidia_warned:
            log.warning("nvidia-smi failed, using IPMI GPU sensors")
            self._nvidia_warned = True
        return self._get_gpu_temps_ipmi()

    def get_ram_temps(self) -> list[float] | None:
        """Get RAM temps via IPMI."""
        temps: list[float] = []
        for sensor in self.config.ram_sensors:
            out = _run_cmd(
                ["ipmitool", "sdr", "get", sensor],
                self.config.cmd_timeout_seconds,
            )
            if out is None:
                continue
            t = self._parse_ipmi_temp(out)
            if t is not None:
                temps.append(t)
        return temps

    def get_hdd_temps(self) -> list[float] | None:
        """Get HDD temps via smartctl."""
        temps: list[float] = []
        for dev in self._hdd_devices:
            out = _run_cmd(
                ["smartctl", "-A", dev],
                self.config.cmd_timeout_seconds,
            )
            if out is None:
                log.warning("Failed to read HDD temp from %s", dev)
                continue
            for line in out.splitlines():
                if (
                    "Temperature_Celsius" not in line
                    and "Airflow_Temperature" not in line
                ):
                    continue
                parts = line.split()
                if len(parts) < 10:
                    continue
                try:
                    t = self._valid_temp(float(parts[9]))
                    if t is not None:
                        temps.append(t)
                except ValueError:
                    pass
        return temps

    def get_nvme_temps(self) -> list[float] | None:
        """Get NVMe temps via nvme-cli."""
        temps: list[float] = []
        for dev in self._nvme_devices:
            out = _run_cmd(
                ["nvme", "smart-log", dev],
                self.config.cmd_timeout_seconds,
            )
            if out is None:
                log.warning("Failed to read NVMe temp from %s", dev)
                continue
            for line in out.splitlines():
                if not line.strip().lower().startswith("temperature"):
                    continue
                parts = line.split(":")
                if len(parts) < 2:
                    continue
                try:
                    t = self._valid_temp(
                        float(parts[1].strip().split()[0].replace(",", ""))
                    )
                    if t is not None:
                        temps.append(t)
                        break
                except (ValueError, IndexError):
                    pass
        return temps

    def set_zone_speed(self, zone: int, percent: int) -> bool:
        """Set fan zone speed."""
        zc = self.config.zones.get(zone)
        if zc is not None:
            percent = max(zc.min_speed_percent, min(zc.max_speed_percent, percent))
        if self.current_speeds.get(zone) == percent:
            return True
        if not self._ensure_full_mode():
            return False
        out = _run_cmd(
            [
                "ipmitool",
                "raw",
                "0x30",
                "0x70",
                "0x66",
                "0x01",
                f"0x{zone:02x}",
                f"0x{percent:02x}",
            ],
            self.config.cmd_timeout_seconds,
        )
        if out is not None:
            self.current_speeds[zone] = percent
            return True
        log.error("Failed to set zone %d to %d%%", zone, percent)
        return False

    def set_full_speed(self) -> bool:
        """Set all zones to 100%."""
        return all(self.set_zone_speed(z, 100) for z in self.config.zones)

    def detect_gpus(self) -> int:
        """Detect GPU count."""
        temps = self.get_gpu_temps()
        return len(temps) if temps else 0

    def _valid_temp(self, value: float) -> float | None:
        """Return value if in valid range, else None."""
        if (
            self.config.temp_min_valid_celsius
            <= value
            <= self.config.temp_max_valid_celsius
        ):
            return value
        return None

    def _parse_ipmi_temp(self, output: str) -> float | None:
        """Parse IPMI sensor output for temperature reading."""
        for line in output.splitlines():
            if "Sensor Reading" not in line:
                continue
            parts = line.split(":")
            if len(parts) < 2:
                continue
            try:
                return self._valid_temp(float(parts[1].strip().split()[0]))
            except (ValueError, IndexError):
                pass
        return None

    def _parse_nvidia_temps(self, output: str) -> list[float] | None:
        """Parse nvidia-smi temperature output. Returns None if any line fails."""
        temps: list[float] = []
        for line in output.strip().splitlines():
            try:
                t = self._valid_temp(float(line.strip()))
                if t is None:
                    return None
                temps.append(t)
            except ValueError:
                return None
        return temps

    def _get_gpu_temps_ipmi(self) -> list[float] | None:
        """Get GPU temps via IPMI sensors."""
        temps: list[float] = []
        any_ok = False
        for i in range(1, self.config.gpu_slots + 1):
            out = _run_cmd(
                ["ipmitool", "sdr", "get", "GPU%d Temp" % i],
                self.config.cmd_timeout_seconds,
            )
            if out is None:
                continue
            any_ok = True
            t = self._parse_ipmi_temp(out)
            if t is not None:
                temps.append(t)
        return temps if any_ok else None

    def _ensure_full_mode(self) -> bool:
        """Ensure BMC is in full/manual fan mode."""
        timeout = self.config.cmd_timeout_seconds
        out = _run_cmd(
            ["ipmitool", "raw", "0x30", "0x45", "0x00"],
            timeout,
        )
        if out is None or out.strip() != "01":
            if (
                _run_cmd(
                    ["ipmitool", "raw", "0x30", "0x45", "0x01", "0x01"],
                    timeout,
                )
                is None
            ):
                log.error("Failed to set full fan mode")
                return False
            time.sleep(0.5)
        return True


class FanDaemon:
    """Main fan control daemon."""

    config: Config
    hardware: Hardware
    running: bool
    # Maps (device_type, device_idx, zone) -> active threshold temperature
    active_thresholds: dict[tuple[str, int, int], float]

    def __init__(self, config: Config, hardware: Hardware) -> None:
        self.config = config
        self.hardware = hardware
        self.running = False
        self.active_thresholds = {}

    @staticmethod
    def _quantize_speed(speed: float, step: int) -> int:
        """Quantize speed to discrete steps."""
        return int(round(speed / step) * step)

    def get_all_temps(self) -> Temps | None:
        """Get all temperatures. Returns None on failure."""
        cpus = self.hardware.get_cpu_temps()
        if cpus is None:
            return None
        gpus = self.hardware.get_gpu_temps()
        if gpus is None:
            return None
        rams = self.hardware.get_ram_temps()
        hdds = self.hardware.get_hdd_temps()
        nvmes = self.hardware.get_nvme_temps()
        if rams is None or hdds is None or nvmes is None:
            return None
        return Temps(
            cpus_celsius=cpus,
            gpus_celsius=gpus,
            rams_celsius=rams,
            hdds_celsius=hdds,
            nvmes_celsius=nvmes,
        )

    def compute_zone_speeds(
        self,
        temps: Temps,
    ) -> dict[int, tuple[int, str, float]]:
        """Compute fan speed per zone. Returns {zone: (speed, trigger, temp)}."""
        cfg = self.config
        device_temps = [
            ("cpu", temps.cpus_celsius),
            ("gpu", temps.gpus_celsius),
            ("ram", temps.rams_celsius),
            ("hdd", temps.hdds_celsius),
            ("nvme", temps.nvmes_celsius),
        ]
        results: dict[int, tuple[int, str, float]] = {}
        for zone, zc in cfg.zones.items():
            candidates: list[tuple[float, str, float, str, int, float]] = []
            for name, temp_list in device_temps:
                for idx, temp in enumerate(temp_list):
                    if (m := cfg.mappings.get(name, idx, zone)) is not None:
                        key = (name, idx, zone)
                        active_thresh = self.active_thresholds.get(key)
                        speed, new_thresh = Mappings.lookup(
                            temp, m, active_thresh, cfg.hysteresis_celsius
                        )
                        candidates.append(
                            (
                                speed,
                                "%s%d" % (name.upper(), idx),
                                temp,
                                name,
                                idx,
                                new_thresh,
                            )
                        )
            if candidates:
                raw_speed, trigger, temp, dev_name, dev_idx, new_thresh = max(
                    candidates, key=lambda x: x[0]
                )
                # Update active threshold for the winning device
                self.active_thresholds[(dev_name, dev_idx, zone)] = new_thresh
                speed = max(
                    zc.min_speed_percent,
                    min(
                        zc.max_speed_percent,
                        self._quantize_speed(raw_speed, zc.speed_step_percent),
                    ),
                )
                results[zone] = (speed, trigger, temp)
            else:
                results[zone] = (zc.min_speed_percent, "none", 0.0)
        return results

    def control_loop(self) -> None:
        """Main control loop iteration."""
        temps = self.get_all_temps()
        if temps is None:
            log.error("Failed to read temps, going to full speed")
            _ = self.hardware.set_full_speed()
            self.active_thresholds.clear()
            return

        zone_speeds = self.compute_zone_speeds(temps)
        changed_zones: list[str] = []

        for zone, (speed, trigger, trigger_temp) in zone_speeds.items():
            if not self.hardware.set_zone_speed(zone, speed):
                _ = self.hardware.set_full_speed()
                self.active_thresholds.clear()
                return

            changed_zones.append(f"z{zone}:{trigger}={trigger_temp:.0f}C->{speed}%")

        if changed_zones:
            log.info(
                "%s [cpu=%s gpu=%s ram=%s hdd=%s nvme=%s]",
                " ".join(changed_zones),
                "/".join(f"{t:.0f}" for t in temps.cpus_celsius) or "-",
                "/".join(f"{t:.0f}" for t in temps.gpus_celsius) or "-",
                "/".join(f"{t:.0f}" for t in temps.rams_celsius) or "-",
                "/".join(f"{t:.0f}" for t in temps.hdds_celsius) or "-",
                "/".join(f"{t:.0f}" for t in temps.nvmes_celsius) or "-",
            )

    def shutdown(
        self,
        signum: int | None = None,
        _frame: object = None,
    ) -> None:
        """Clean shutdown - set fans to full."""
        log.info("Shutting down (signal %d)", signum or 0)
        self.running = False
        _ = self.hardware.set_full_speed()
        sys.exit(0)

    def run(self) -> None:
        """Main daemon loop."""
        _ = signal.signal(signal.SIGTERM, self.shutdown)
        _ = signal.signal(signal.SIGINT, self.shutdown)

        cfg = self.config
        log.info(
            "Starting: zones=%s gpus=%d",
            list(cfg.zones.keys()),
            self.hardware.detect_gpus(),
        )

        if not self.hardware.set_full_speed():
            log.error("Failed to set initial fan speed")

        self.running = True
        while self.running:
            try:
                self.control_loop()
            except Exception:
                log.exception("Control loop error")
                _ = self.hardware.set_full_speed()

            time.sleep(cfg.interval_seconds)

        _ = self.hardware.set_full_speed()


def main() -> None:
    config = Config.from_args()
    daemon = FanDaemon(config, Supermicro(config))
    daemon.run()


if __name__ == "__main__":
    main()

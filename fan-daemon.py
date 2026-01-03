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
from typing import Protocol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fan-daemon")

DeviceCelsiusToFanZonePercent = tuple[tuple[float, float], ...]
MappingKey = tuple[str, int, int]  # (device_type, device_idx, zone); -1 means "all"


class Mappings:
    """Temperature-to-fan-speed mapping lookup with precedence."""

    mappings: dict[MappingKey, DeviceCelsiusToFanZonePercent | None]

    def __init__(
        self,
        overrides: dict[MappingKey, DeviceCelsiusToFanZonePercent | None] | None = None,
    ):
        self.mappings = {
            ("cpu", -1, 0): (
                (40, 15),
                (60, 30),
                (75, 60),
                (85, 100),
            ),
            ("gpu", -1, -1): (
                (50, 15),
                (70, 20),
                (80, 50),
                (85, 100),
            ),
            ("ram", -1, 0): (
                (40, 15),
                (60, 25),
                (70, 50),
                (80, 100),
            ),
            ("hdd", -1, 0): (
                (25, 15),
                (40, 25),
                (45, 50),
                (50, 100),
            ),
            ("nvme", -1, 0): (
                (35, 15),
                (50, 30),
                (60, 60),
                (70, 100),
            ),
        }
        if overrides:
            self.mappings.update(overrides)

    def get(
        self, device_type: str, device_idx: int, zone: int
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
        """Parse "temp:speed,temp:speed,..." into mapping. Empty string -> None (disabled)."""
        s = s.strip()
        if not s:
            return None
        points: list[tuple[float, float]] = []
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            t, sp = part.split(":")
            temp, speed = float(t), float(sp)
            if not 0 <= speed <= 100:
                raise ValueError("Speed must be 0-100, got %s" % speed)
            points.append((temp, speed))
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
            r"^(cpu|gpu|hdd|nvme)(\d+)?-zone(\d+)?$",
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
    def lookup(cls, temp: float, mapping: DeviceCelsiusToFanZonePercent) -> float:
        """Piecewise constant lookup - return speed for highest threshold <= temp."""
        for t, s in reversed(mapping):
            if temp >= t:
                return s
        return mapping[0][1]


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
        default_factory=lambda: {0: ZoneConfig(), 1: ZoneConfig()}
    )
    sensitivity_celsius: float = 2.0
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
Mapping format: DEVICE[N]-zone[M]=TEMP:SPEED,TEMP:SPEED,...
  Examples:
    --mapping gpu-zone=50:15,85:100       All GPUs, all zones
    --mapping gpu-zone0=50:15,85:100      All GPUs, zone 0 only
    --mapping gpu0-zone1=60:20,85:100     GPU #0, zone 1 only
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
            "--sensitivity",
            type=float,
            default=2.0,
            help="Ignore temp changes smaller than (C).",
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
        ns = vars(p.parse_args())
        min_speed = int(ns["min_speed"])  # pyright: ignore[reportAny]
        max_speed = int(ns["max_speed"])  # pyright: ignore[reportAny]
        if min_speed >= max_speed:
            p.error("--min-speed must be less than --max-speed")
        user_mappings: dict[MappingKey, DeviceCelsiusToFanZonePercent | None] = {}
        for spec in ns.get("mapping") or []:  # pyright: ignore[reportUnknownVariableType]
            try:
                key, mapping = Mappings.parse_spec(str(spec))  # pyright: ignore[reportUnknownArgumentType]
                user_mappings[key] = mapping
            except ValueError as e:
                p.error(str(e))
        hdd_str = str(ns["hdd_devices"])  # pyright: ignore[reportAny]
        nvme_str = str(ns["nvme_devices"])  # pyright: ignore[reportAny]
        hdd: tuple[str, ...] | None = None  # auto-detect
        nvme: tuple[str, ...] | None = None  # auto-detect
        if hdd_str:
            hdd = tuple(x.strip() for x in hdd_str.split(",") if x.strip())
        if nvme_str:
            nvme = tuple(x.strip() for x in nvme_str.split(",") if x.strip())
        speed_step = int(ns["speed_step"])  # pyright: ignore[reportAny]
        sensitivity = float(ns["sensitivity"])  # pyright: ignore[reportAny]
        interval = float(ns["interval"])  # pyright: ignore[reportAny]
        gpu_slots = int(ns["gpu_slots"])  # pyright: ignore[reportAny]
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
            sensitivity_celsius=sensitivity,
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
    last_temps: dict[int, float]

    def __init__(self, config: Config, hardware: Hardware) -> None:
        self.config = config
        self.hardware = hardware
        self.running = False
        self.last_temps = {}

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
            ("CPU", temps.cpus_celsius),
            ("GPU", temps.gpus_celsius),
            ("RAM", temps.rams_celsius),
            ("HDD", temps.hdds_celsius),
            ("NVMe", temps.nvmes_celsius),
        ]
        results: dict[int, tuple[int, str, float]] = {}
        for zone, zc in cfg.zones.items():
            candidates: list[tuple[float, str, float]] = []
            for name, temp_list in device_temps:
                for idx, temp in enumerate(temp_list):
                    if (m := cfg.mappings.get(name.lower(), idx, zone)) is not None:
                        candidates.append(
                            (Mappings.lookup(temp, m), "%s%d" % (name, idx), temp)
                        )
            if candidates:
                raw_speed, trigger, temp = max(candidates, key=lambda x: x[0])
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
            self.last_temps.clear()
            return

        zone_speeds = self.compute_zone_speeds(temps)
        changed_zones: list[str] = []

        for zone, (speed, trigger, trigger_temp) in zone_speeds.items():
            last_temp = self.last_temps.get(zone)
            if (
                last_temp is not None
                and abs(trigger_temp - last_temp) < self.config.sensitivity_celsius
            ):
                continue

            if not self.hardware.set_zone_speed(zone, speed):
                _ = self.hardware.set_full_speed()
                self.last_temps.clear()
                return

            self.last_temps[zone] = trigger_temp
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

"""Standalone sensor classes for reading temperatures from various sources.

Each sensor implements get() -> dict[str, tuple[float, ...] | None]
"""

from __future__ import annotations

import logging
import pathlib
import subprocess
from typing import Protocol

log = logging.getLogger("fan-daemon")

# Type alias for sensor return values
SensorResult = dict[str, tuple[float, ...] | None]


class Sensor(Protocol):
    """Protocol for temperature sensors."""

    def get(self) -> SensorResult:
        """Read temperatures. Returns {device_type: (temps...) | None}."""
        ...


class K10Temp:
    """AMD CPU temperature sensor via k10temp hwmon."""

    def __init__(self) -> None:
        self._hwmon_path: pathlib.Path | None = None
        for hwmon in pathlib.Path("/sys/class/hwmon").iterdir():
            name_file = hwmon / "name"
            if name_file.exists() and name_file.read_text().strip() == "k10temp":
                self._hwmon_path = hwmon
                break
        if self._hwmon_path is None:
            log.warning("k10temp hwmon not found")

    def get(self) -> SensorResult:
        """Read CPU temps from k10temp hwmon."""
        if self._hwmon_path is None:
            return {"cpu": None}

        temps: list[float] = []
        for temp_input in sorted(self._hwmon_path.glob("temp*_input")):
            try:
                millidegrees = int(temp_input.read_text().strip())
                temp = _valid_temp(millidegrees / 1000.0)
                if temp is not None:
                    temps.append(temp)
            except (ValueError, OSError):
                pass

        return {"cpu": tuple(temps) if temps else None}


class Nvidiasmi:
    """NVIDIA GPU temperature sensor via nvidia-smi."""

    def get(self) -> SensorResult:
        """Read GPU temps from nvidia-smi."""
        out = run_cmd(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ]
        )
        if out is None:
            return {"gpu": None}

        temps: list[float] = []
        for line in out.strip().splitlines():
            try:
                temp = _valid_temp(float(line.strip()))
                if temp is not None:
                    temps.append(temp)
                else:
                    return {"gpu": None}
            except ValueError:
                return {"gpu": None}

        return {"gpu": tuple(temps) if temps else None}


class Smartctl:
    """HDD temperature sensor via smartctl."""

    _devices: tuple[str, ...]

    def __init__(self) -> None:
        # Auto-detect HDDs (rotational disks)
        hdds: list[str] = []
        for block in pathlib.Path("/sys/block").iterdir():
            if not block.name.startswith("sd"):
                continue
            rotational = block / "queue" / "rotational"
            if rotational.exists() and rotational.read_text().strip() == "1":
                hdds.append(f"/dev/{block.name}")
        self._devices = tuple(sorted(hdds))
        if self._devices:
            log.info("HDDs: %s", ", ".join(self._devices))

    def get(self) -> SensorResult:
        """Read HDD temps via smartctl."""
        if not self._devices:
            return {"hdd": None}

        temps: list[float] = []
        for dev in self._devices:
            out = run_cmd(["smartctl", "-A", dev])
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
                    temp = _valid_temp(float(parts[9]))
                    if temp is not None:
                        temps.append(temp)
                except ValueError:
                    pass

        return {"hdd": tuple(temps) if temps else None}


class Nvmecli:
    """NVMe temperature sensor via nvme-cli."""

    _devices: tuple[str, ...]

    def __init__(self) -> None:
        # Auto-detect NVMe devices
        nvmes: list[str] = []
        for dev in pathlib.Path("/dev").glob("nvme*n1"):
            nvmes.append(str(dev))
        self._devices = tuple(sorted(nvmes))
        if self._devices:
            log.info("NVMe: %s", ", ".join(self._devices))

    def get(self) -> SensorResult:
        """Read NVMe temps via nvme-cli."""
        if not self._devices:
            return {"nvme": None}

        temps: list[float] = []
        for dev in self._devices:
            out = run_cmd(["nvme", "smart-log", dev])
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
                    temp = _valid_temp(
                        float(parts[1].strip().split()[0].replace(",", ""))
                    )
                    if temp is not None:
                        temps.append(temp)
                        break  # Only first temperature line per device
                except (ValueError, IndexError):
                    pass

        return {"nvme": tuple(temps) if temps else None}


class Ipmitool:
    """IPMI temperature sensors via ipmitool."""

    _sensor_map: dict[str, str]

    def __init__(self, sensor_map: dict[str, str]) -> None:
        """Initialize with sensor name to key mapping.

        Args:
            sensor_map: Mapping of IPMI sensor names to result keys.
                e.g., {"CPU Temp": "cpu", "DIMMA~F Temp": "ram"}
        """
        self._sensor_map = sensor_map

    def get(self) -> SensorResult:
        """Read temps from ipmitool sensor."""
        out = run_cmd(["ipmitool", "sensor"])
        if out is None:
            log.error("Failed to run ipmitool sensor")
            keys = set(self._sensor_map.values())
            return {k: None for k in keys}

        # Collect temps as lists first
        temps: dict[str, list[float]] = {}
        for key in set(self._sensor_map.values()):
            temps[key] = []

        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 2:
                continue
            sensor_name = parts[0].strip()
            if sensor_name not in self._sensor_map:
                continue
            key = self._sensor_map[sensor_name]
            value_str = parts[1].strip()
            try:
                temp = _valid_temp(float(value_str))
                if temp is not None:
                    temps[key].append(temp)
            except ValueError:
                pass  # "na" or other non-numeric

        return {k: tuple(v) if v else None for k, v in temps.items()}


def run_cmd(cmd: list[str], timeout: float = 5.0) -> str | None:
    """Run command with timeout. Returns stdout on success, None on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _valid_temp(value: float) -> float | None:
    """Return value if in valid range (0-120C), else None."""
    if 0 <= value <= 120:
        return value
    return None

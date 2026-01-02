#!/usr/bin/env python3
"""
Fan daemon for server motherboards using linear temperature mapping.

Reads temps from: CPU (IPMI), GPU (nvidia-smi/IPMI), HDD (smartctl), NVMe (nvme-cli)
Uses max temp across all sources as input.
Linear maps temp_min->temp_max to min_speed->max_speed.
Sensitivity dead-band prevents hunting on small temp changes.

Fail-safe: Any error -> full speed (100%)

Run with --help for configuration options.
"""

import abc
import argparse
import dataclasses
import logging
import signal
import subprocess
import sys
import time
import types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fan-daemon")


@dataclasses.dataclass(slots=True)
class Temps:
    """Temperature readings in Celsius."""

    cpu_celsius: float
    gpus_celsius: list[float]
    hdds_celsius: list[float]
    nvmes_celsius: list[float]

    @property
    def max_celsius(self) -> float:
        """Return max temp across all sources."""
        all_temps = [self.cpu_celsius] + self.gpus_celsius + self.hdds_celsius + self.nvmes_celsius
        return max(all_temps)


@dataclasses.dataclass(slots=True)
class Config:
    """Daemon configuration."""

    temp_min_celsius: float = 40.0
    temp_max_celsius: float = 80.0
    min_speed_percent: int = 15
    max_speed_percent: int = 100
    speed_step_percent: int = 10
    sensitivity_celsius: float = 2.0
    interval_seconds: float = 5.0
    gpu_slots: int = 5
    hdd_devices: tuple[str, ...] = ()
    nvme_devices: tuple[str, ...] = ()
    temp_min_valid_celsius: float = 0.0
    temp_max_valid_celsius: float = 120.0
    cmd_timeout_seconds: float = 5.0


def parse_args() -> Config:
    """Parse command-line arguments and return Config."""
    parser = argparse.ArgumentParser(
        description="Fan daemon for server motherboards",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    d = Config()
    parser.add_argument(
        "--temp-min",
        type=type(d.temp_min_celsius),
        default=d.temp_min_celsius,
        help="Temp for min fan speed (C)",
    )
    parser.add_argument(
        "--temp-max",
        type=type(d.temp_max_celsius),
        default=d.temp_max_celsius,
        help="Temp for max fan speed (C)",
    )
    parser.add_argument(
        "--min-speed",
        type=type(d.min_speed_percent),
        default=d.min_speed_percent,
        help="Minimum fan speed (%%)",
    )
    parser.add_argument(
        "--max-speed",
        type=type(d.max_speed_percent),
        default=d.max_speed_percent,
        help="Maximum fan speed (%%)",
    )
    parser.add_argument(
        "--speed-step",
        type=type(d.speed_step_percent),
        default=d.speed_step_percent,
        help="Fan speed step size (%%)",
    )
    parser.add_argument(
        "--sensitivity",
        type=type(d.sensitivity_celsius),
        default=d.sensitivity_celsius,
        help="Ignore temp changes smaller than this (C)",
    )
    parser.add_argument(
        "--interval",
        type=type(d.interval_seconds),
        default=d.interval_seconds,
        help="Poll interval (seconds)",
    )
    parser.add_argument(
        "--gpu-slots",
        type=type(d.gpu_slots),
        default=d.gpu_slots,
        help="Number of PCIe GPU slots for IPMI fallback",
    )
    parser.add_argument(
        "--hdd-devices",
        type=str,
        default="",
        help="Comma-separated HDD paths (e.g., /dev/sda,/dev/sdb)",
    )
    parser.add_argument(
        "--nvme-devices",
        type=str,
        default="",
        help="Comma-separated NVMe paths (e.g., /dev/nvme0)",
    )
    args = parser.parse_args()
    if args.temp_min >= args.temp_max:
        parser.error("--temp-min must be less than --temp-max")
    if args.min_speed >= args.max_speed:
        parser.error("--min-speed must be less than --max-speed")
    hdd_devices = tuple(d.strip() for d in args.hdd_devices.split(",") if d.strip())
    nvme_devices = tuple(d.strip() for d in args.nvme_devices.split(",") if d.strip())
    return Config(
        temp_min_celsius=args.temp_min,
        temp_max_celsius=args.temp_max,
        min_speed_percent=args.min_speed,
        max_speed_percent=args.max_speed,
        speed_step_percent=args.speed_step,
        sensitivity_celsius=args.sensitivity,
        interval_seconds=args.interval,
        gpu_slots=args.gpu_slots,
        hdd_devices=hdd_devices,
        nvme_devices=nvme_devices,
    )


class HardwareInterface(abc.ABC):
    """Abstract interface for hardware operations."""

    @abc.abstractmethod
    def read_cpu_temp(self) -> float | None:
        """Read CPU temperature. Returns None on failure."""

    @abc.abstractmethod
    def read_gpu_temps(self) -> list[float] | None:
        """Read GPU temperatures. Returns [] if no GPUs, None on failure."""

    @abc.abstractmethod
    def read_hdd_temps(self) -> list[float]:
        """Read HDD temperatures. Returns [] if none configured."""

    @abc.abstractmethod
    def read_nvme_temps(self) -> list[float]:
        """Read NVMe temperatures. Returns [] if none configured."""

    @abc.abstractmethod
    def set_zone_speed(self, zone: int, percent: int) -> bool:
        """Set fan zone speed. Returns success."""

    @abc.abstractmethod
    def set_full_speed(self) -> bool:
        """Set all zones to 100%. Returns success."""

    @abc.abstractmethod
    def detect_gpus(self) -> tuple[int, str]:
        """Detect GPUs and return (count, source)."""


class SupermicroHardware(HardwareInterface):
    """Hardware interface for Supermicro motherboards."""

    CMD_GET_FAN_MODE = ["ipmitool", "raw", "0x30", "0x45", "0x00"]
    CMD_SET_FULL_MODE = ["ipmitool", "raw", "0x30", "0x45", "0x01", "0x01"]
    FAN_MODE_FULL = "01"
    MODE_SWITCH_DELAY_SECONDS = 0.5
    SENSOR_CPU_TEMP = "CPU Temp"
    SENSOR_GPU_TEMP_FMT = "GPU{} Temp"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.current_speeds: dict[int, int | None] = {0: None, 1: None}
        self._nvidia_warned = False

    def _run_cmd(self, cmd: list[str]) -> tuple[bool, str]:
        """Run a command with timeout. Returns (success, output)."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.cmd_timeout_seconds,
            )
            if result.returncode != 0:
                return False, result.stderr
            return True, result.stdout
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except Exception as e:
            return False, str(e)

    def _read_ipmi_temp(self, sensor: str) -> float | None:
        """Read temperature from IPMI sensor."""
        success, output = self._run_cmd(["ipmitool", "sdr", "get", sensor])
        if not success:
            return None

        for line in output.splitlines():
            if "Sensor Reading" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    reading = parts[1].strip().split()[0]
                    try:
                        temp = float(reading)
                        if (
                            self.config.temp_min_valid_celsius
                            <= temp
                            <= self.config.temp_max_valid_celsius
                        ):
                            return temp
                    except ValueError:
                        pass
        return None

    def _read_gpu_temps_nvidia(self) -> list[float] | None:
        """Read GPU temps from nvidia-smi."""
        success, output = self._run_cmd(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ]
        )
        if not success:
            return None

        temps = []
        for line in output.strip().splitlines():
            try:
                temp = float(line.strip())
                if (
                    self.config.temp_min_valid_celsius
                    <= temp
                    <= self.config.temp_max_valid_celsius
                ):
                    temps.append(temp)
                else:
                    return None
            except ValueError:
                return None

        return temps

    def _read_gpu_temps_ipmi(self) -> list[float] | None:
        """Read GPU temps from IPMI. Returns [] if no GPUs, None if IPMI fails."""
        temps = []
        any_success = False
        for i in range(1, self.config.gpu_slots + 1):
            success, output = self._run_cmd(
                ["ipmitool", "sdr", "get", self.SENSOR_GPU_TEMP_FMT.format(i)]
            )
            if success:
                any_success = True
                for line in output.splitlines():
                    if "Sensor Reading" in line:
                        parts = line.split(":")
                        if len(parts) >= 2:
                            try:
                                temp = float(parts[1].strip().split()[0])
                                if (
                                    self.config.temp_min_valid_celsius
                                    <= temp
                                    <= self.config.temp_max_valid_celsius
                                ):
                                    temps.append(temp)
                            except (ValueError, IndexError):
                                pass
        if not any_success:
            return None
        return temps

    def read_cpu_temp(self) -> float | None:
        """Read CPU temperature."""
        temp = self._read_ipmi_temp(self.SENSOR_CPU_TEMP)
        if temp is None:
            log.error("Failed to read %s", self.SENSOR_CPU_TEMP)
        return temp

    def read_gpu_temps(self) -> list[float] | None:
        """Read GPU temps, trying nvidia-smi first, then IPMI fallback."""
        temps = self._read_gpu_temps_nvidia()
        if temps is not None:
            return temps
        if not self._nvidia_warned:
            log.warning("nvidia-smi failed, using IPMI GPU sensors")
            self._nvidia_warned = True
        return self._read_gpu_temps_ipmi()

    def read_hdd_temps(self) -> list[float]:
        """Read HDD temps via smartctl."""
        temps = []
        for device in self.config.hdd_devices:
            success, output = self._run_cmd(["smartctl", "-A", device])
            if not success:
                log.warning("Failed to read HDD temp from %s", device)
                continue
            for line in output.splitlines():
                if "Temperature_Celsius" in line or "Airflow_Temperature" in line:
                    parts = line.split()
                    if len(parts) >= 10:
                        try:
                            temp = float(parts[9])
                            if (
                                self.config.temp_min_valid_celsius
                                <= temp
                                <= self.config.temp_max_valid_celsius
                            ):
                                temps.append(temp)
                        except ValueError:
                            pass
        return temps

    def read_nvme_temps(self) -> list[float]:
        """Read NVMe temps via nvme-cli."""
        temps = []
        for device in self.config.nvme_devices:
            success, output = self._run_cmd(["nvme", "smart-log", device])
            if not success:
                log.warning("Failed to read NVMe temp from %s", device)
                continue
            for line in output.splitlines():
                if line.strip().lower().startswith("temperature"):
                    parts = line.split(":")
                    if len(parts) >= 2:
                        try:
                            temp_str = parts[1].strip().split()[0]
                            temp = float(temp_str.replace(",", ""))
                            if (
                                self.config.temp_min_valid_celsius
                                <= temp
                                <= self.config.temp_max_valid_celsius
                            ):
                                temps.append(temp)
                                break
                        except (ValueError, IndexError):
                            pass
        return temps

    def _ensure_full_mode(self) -> bool:
        """Ensure BMC is in full/manual fan mode."""
        success, output = self._run_cmd(self.CMD_GET_FAN_MODE)
        if not success or output.strip() != self.FAN_MODE_FULL:
            success, _ = self._run_cmd(self.CMD_SET_FULL_MODE)
            if not success:
                log.error("Failed to set full fan mode")
                return False
            time.sleep(self.MODE_SWITCH_DELAY_SECONDS)
        return True

    def set_zone_speed(self, zone: int, percent: int) -> bool:
        """Set fan zone speed."""
        percent = max(
            self.config.min_speed_percent,
            min(self.config.max_speed_percent, percent),
        )
        if self.current_speeds.get(zone) == percent:
            return True
        if not self._ensure_full_mode():
            return False

        duty_hex = f"0x{percent:02x}"
        zone_hex = f"0x{zone:02x}"
        success, output = self._run_cmd(
            [
                "ipmitool",
                "raw",
                "0x30",
                "0x70",
                "0x66",
                "0x01",
                zone_hex,
                duty_hex,
            ]
        )

        if success:
            self.current_speeds[zone] = percent
            return True
        log.error("Failed to set zone %d to %d%%: %s", zone, percent, output)
        return False

    def set_full_speed(self) -> bool:
        """Set all zones to 100%."""
        ok0 = self.set_zone_speed(0, 100)
        ok1 = self.set_zone_speed(1, 100)
        return ok0 and ok1

    def detect_gpus(self) -> tuple[int, str]:
        """Detect GPUs and return (count, source)."""
        temps = self._read_gpu_temps_nvidia()
        if temps:
            return len(temps), "nvidia-smi"
        temps = self._read_gpu_temps_ipmi()
        if temps:
            return len(temps), "IPMI"
        return 0, "none"


def linear_map(
    value: float,
    in_min: float,
    in_max: float,
    out_min: float,
    out_max: float,
) -> float:
    """Linear interpolation with clamping."""
    if value <= in_min:
        return out_min
    if value >= in_max:
        return out_max
    return (value - in_min) / (in_max - in_min) * (out_max - out_min) + out_min


class FanDaemon:
    """Main fan control daemon."""

    def __init__(self, config: Config, hardware: HardwareInterface) -> None:
        self.config = config
        self.hardware = hardware
        self.running = False
        self.last_temp: float | None = None
        self.last_speed: int | None = None

    def _quantize_speed(self, speed: float) -> int:
        """Quantize speed to discrete steps."""
        step = self.config.speed_step_percent
        return int(round(speed / step) * step)

    def read_all_temps(self) -> Temps | None:
        """Read all temperatures. Returns None on failure."""
        cpu = self.hardware.read_cpu_temp()
        if cpu is None:
            return None
        gpus = self.hardware.read_gpu_temps()
        if gpus is None:
            return None
        hdds = self.hardware.read_hdd_temps()
        nvmes = self.hardware.read_nvme_temps()
        return Temps(
            cpu_celsius=cpu,
            gpus_celsius=gpus,
            hdds_celsius=hdds,
            nvmes_celsius=nvmes,
        )

    def control_loop(self) -> None:
        """Main control loop iteration."""
        temps = self.read_all_temps()
        if temps is None:
            log.error("Failed to read temps, going to full speed")
            self.hardware.set_full_speed()
            self.last_temp = None
            self.last_speed = None
            return

        cfg = self.config
        max_temp = temps.max_celsius

        # Sensitivity check: skip if temp change is below threshold
        if (
            self.last_temp is not None
            and self.last_speed is not None
            and abs(max_temp - self.last_temp) < cfg.sensitivity_celsius
        ):
            return

        # Linear map from temp range to speed range
        speed = self._quantize_speed(
            linear_map(
                max_temp,
                cfg.temp_min_celsius,
                cfg.temp_max_celsius,
                cfg.min_speed_percent,
                cfg.max_speed_percent,
            )
        )

        # Set both zones to same speed
        if not self.hardware.set_zone_speed(0, speed):
            self.hardware.set_full_speed()
            return
        if not self.hardware.set_zone_speed(1, speed):
            self.hardware.set_full_speed()
            return

        self.last_temp = max_temp
        self.last_speed = speed

        gpu_str = "/".join(f"{t:.0f}" for t in temps.gpus_celsius)
        hdd_str = "/".join(f"{t:.0f}" for t in temps.hdds_celsius)
        nvme_str = "/".join(f"{t:.0f}" for t in temps.nvmes_celsius)
        log.info(
            "cpu=%.0f gpu=[%s] hdd=[%s] nvme=[%s] max=%.0f -> %d%%",
            temps.cpu_celsius,
            gpu_str,
            hdd_str,
            nvme_str,
            max_temp,
            speed,
        )

    def shutdown(
        self,
        signum: int | None = None,
        _frame: types.FrameType | None = None,
    ) -> None:
        """Clean shutdown - set fans to full."""
        log.info("Shutting down (signal %s)", signum)
        self.running = False
        self.hardware.set_full_speed()
        sys.exit(0)

    def run(self) -> None:
        """Main daemon loop."""
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)

        cfg = self.config
        log.info("Fan daemon starting")
        log.info(
            "Config: temp=%s-%s speed=%s-%s step=%s sensitivity=%s",
            cfg.temp_min_celsius,
            cfg.temp_max_celsius,
            cfg.min_speed_percent,
            cfg.max_speed_percent,
            cfg.speed_step_percent,
            cfg.sensitivity_celsius,
        )

        gpu_count, gpu_source = self.hardware.detect_gpus()
        if gpu_count > 0:
            log.info("Detected %d GPU(s) via %s", gpu_count, gpu_source)
        else:
            log.info("No GPUs detected")
        if cfg.hdd_devices:
            log.info("HDD devices: %s", ", ".join(cfg.hdd_devices))
        if cfg.nvme_devices:
            log.info("NVMe devices: %s", ", ".join(cfg.nvme_devices))

        if not self.hardware.set_full_speed():
            log.error("Failed to set initial fan speed")

        self.running = True
        while self.running:
            try:
                self.control_loop()
            except Exception:
                log.exception("Control loop error")
                self.hardware.set_full_speed()

            time.sleep(cfg.interval_seconds)

        self.hardware.set_full_speed()


def main() -> None:
    config = parse_args()
    hardware = SupermicroHardware(config)
    daemon = FanDaemon(config, hardware)
    daemon.run()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
PID-controlled fan daemon for server motherboards.

Zones:
  Zone 0: CPU cooler + case fans, input: max(CPU, hottest GPU)
  Zone 1: Auxiliary fans (GPU/RAM), input: hottest GPU

GPU temps: nvidia-smi preferred, IPMI fallback
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

    @property
    def gpu_max_celsius(self) -> float | None:
        return max(self.gpus_celsius) if self.gpus_celsius else None


@dataclasses.dataclass(slots=True)
class Config:
    """Daemon configuration."""

    gpu_target_celsius: float = 75.0
    cpu_target_celsius: float = 65.0
    min_speed_percent: int = 15
    max_speed_percent: int = 100
    pid_proportional: float = 3.0
    pid_integral: float = 0.1
    pid_derivative: float = 1.0
    speed_step_percent: int = 10
    interval_seconds: float = 5.0
    gpu_slots: int = 5
    temp_min_valid_celsius: float = 0.0
    temp_max_valid_celsius: float = 120.0
    cmd_timeout_seconds: float = 5.0


def parse_args() -> Config:
    """Parse command-line arguments and return Config."""
    parser = argparse.ArgumentParser(
        description="PID-controlled fan daemon for server motherboards",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    d = Config()
    parser.add_argument(
        "--gpu-target",
        type=type(d.gpu_target_celsius),
        default=d.gpu_target_celsius,
        help="GPU target temperature (C)",
    )
    parser.add_argument(
        "--cpu-target",
        type=type(d.cpu_target_celsius),
        default=d.cpu_target_celsius,
        help="CPU target temperature (C)",
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
        "--pid-proportional",
        type=type(d.pid_proportional),
        default=d.pid_proportional,
        help="PID proportional gain",
    )
    parser.add_argument(
        "--pid-integral",
        type=type(d.pid_integral),
        default=d.pid_integral,
        help="PID integral gain",
    )
    parser.add_argument(
        "--pid-derivative",
        type=type(d.pid_derivative),
        default=d.pid_derivative,
        help="PID derivative gain",
    )
    parser.add_argument(
        "--speed-step",
        type=type(d.speed_step_percent),
        default=d.speed_step_percent,
        help="Fan speed step size (%%)",
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
    args = parser.parse_args()
    return Config(
        gpu_target_celsius=args.gpu_target,
        cpu_target_celsius=args.cpu_target,
        min_speed_percent=args.min_speed,
        max_speed_percent=args.max_speed,
        pid_proportional=args.pid_proportional,
        pid_integral=args.pid_integral,
        pid_derivative=args.pid_derivative,
        speed_step_percent=args.speed_step,
        interval_seconds=args.interval,
        gpu_slots=args.gpu_slots,
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

    def _ensure_full_mode(self) -> bool:
        """Ensure BMC is in full/manual fan mode."""
        success, output = self._run_cmd(self.CMD_GET_FAN_MODE)
        if success:
            if output.strip() != self.FAN_MODE_FULL:
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


class PIDController:
    """PID controller with anti-windup."""

    EPSILON = 1e-6

    def __init__(
        self,
        kp: float,
        ki: float,
        kd: float,
        output_min: float,
        output_max: float,
    ) -> None:
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_min = output_min
        self.output_max = output_max
        self.integral = 0.0
        self.prev_error = 0.0
        self.first_run = True

    def update(self, error: float, dt: float) -> float:
        """Compute PID output given error and time delta."""
        p_term = self.kp * error

        self.integral += error * dt
        max_integral = (self.output_max - self.output_min) / (self.ki + self.EPSILON)
        self.integral = max(-max_integral, min(max_integral, self.integral))
        i_term = self.ki * self.integral

        if self.first_run:
            d_term = 0.0
            self.first_run = False
        else:
            d_term = self.kd * (error - self.prev_error) / dt
        self.prev_error = error

        output = p_term + i_term + d_term
        return max(self.output_min, min(self.output_max, output))

    def reset(self) -> None:
        """Reset controller state."""
        self.integral = 0.0
        self.prev_error = 0.0
        self.first_run = True


class FanDaemon:
    """Main fan control daemon."""

    def __init__(self, config: Config, hardware: HardwareInterface) -> None:
        self.config = config
        self.hardware = hardware
        self.running = False
        self.pid_zone0 = PIDController(
            config.pid_proportional,
            config.pid_integral,
            config.pid_derivative,
            config.min_speed_percent,
            config.max_speed_percent,
        )
        self.pid_zone1 = PIDController(
            config.pid_proportional,
            config.pid_integral,
            config.pid_derivative,
            config.min_speed_percent,
            config.max_speed_percent,
        )
        self.last_time: float | None = None

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
        return Temps(cpu_celsius=cpu, gpus_celsius=gpus)

    def control_loop(self) -> None:
        """Main control loop iteration."""
        now = time.monotonic()
        dt = (
            self.config.interval_seconds
            if self.last_time is None
            else now - self.last_time
        )
        self.last_time = now

        temps = self.read_all_temps()
        if temps is None:
            log.error("Failed to read temps, going to full speed")
            self.hardware.set_full_speed()
            self.pid_zone0.reset()
            self.pid_zone1.reset()
            return

        cpu_target = self.config.cpu_target_celsius
        gpu_target = self.config.gpu_target_celsius
        gpu_max = temps.gpu_max_celsius

        # Zone 0: max(CPU, hottest GPU) or CPU-only if no GPUs
        zone0_input = max(temps.cpu_celsius, gpu_max) if gpu_max else temps.cpu_celsius
        zone0_speed = self._quantize_speed(
            self.pid_zone0.update(zone0_input - cpu_target, dt)
        )

        # Zone 1: hottest GPU, or track CPU if no GPUs
        zone1_input = gpu_max if gpu_max else temps.cpu_celsius
        zone1_target = gpu_target if gpu_max else cpu_target
        zone1_speed = self._quantize_speed(
            self.pid_zone1.update(zone1_input - zone1_target, dt)
        )

        if not self.hardware.set_zone_speed(0, zone0_speed):
            self.hardware.set_full_speed()
            return
        if not self.hardware.set_zone_speed(1, zone1_speed):
            self.hardware.set_full_speed()
            return

        gpu_str = "/".join(f"{t:.0f}" for t in temps.gpus_celsius)
        log.info(
            "cpu=%.0f gpu=[%s] | targets=%.0f/%.0f | zone0=%d%% zone1=%d%%",
            temps.cpu_celsius,
            gpu_str,
            cpu_target,
            gpu_target,
            zone0_speed,
            zone1_speed,
        )

    def shutdown(
        self,
        signum: int | None = None,
        frame: types.FrameType | None = None,
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

        log.info("Fan daemon starting")
        log.info(
            "Config: gpu_target=%s, cpu_target=%s, PID=%s/%s/%s",
            self.config.gpu_target_celsius,
            self.config.cpu_target_celsius,
            self.config.pid_proportional,
            self.config.pid_integral,
            self.config.pid_derivative,
        )

        gpu_count, gpu_source = self.hardware.detect_gpus()
        if gpu_count > 0:
            log.info("Detected %d GPU(s) via %s", gpu_count, gpu_source)
        else:
            log.info("No GPUs detected")

        if not self.hardware.set_full_speed():
            log.error("Failed to set initial fan speed")

        self.running = True
        while self.running:
            try:
                self.control_loop()
            except Exception:
                log.exception("Control loop error")
                self.hardware.set_full_speed()
                self.pid_zone0.reset()
                self.pid_zone1.reset()

            time.sleep(self.config.interval_seconds)

        self.hardware.set_full_speed()


def main() -> None:
    config = parse_args()
    hardware = SupermicroHardware(config)
    daemon = FanDaemon(config, hardware)
    daemon.run()


if __name__ == "__main__":
    main()

"""Unit tests for fan-daemon.py."""

import sys
import pytest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

_spec = spec_from_loader("fan_daemon", SourceFileLoader("fan_daemon", "fan-daemon.py"))
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)

Config = _module.Config
PIDController = _module.PIDController
FanDaemon = _module.FanDaemon
HardwareInterface = _module.HardwareInterface


class MockHardware(HardwareInterface):
    """Mock hardware for testing."""

    def __init__(self) -> None:
        self.cpu_temp: float | None = 50.0
        self.gpu_temps: list[float] = [70.0, 75.0]
        self.zone_speeds: dict[int, int] = {}
        self.full_speed_called = False

    def read_cpu_temp(self) -> float | None:
        return self.cpu_temp

    def read_gpu_temps(self) -> list[float] | None:
        return self.gpu_temps

    def set_zone_speed(self, zone: int, percent: int) -> bool:
        self.zone_speeds[zone] = percent
        return True

    def set_full_speed(self) -> bool:
        self.full_speed_called = True
        self.zone_speeds[0] = 100
        self.zone_speeds[1] = 100
        return True

    def detect_gpus(self) -> tuple[int, str]:
        return len(self.gpu_temps), "mock"


class TestConfig:
    def test_defaults(self) -> None:
        config = Config()
        assert config.gpu_target_celsius == 75.0
        assert config.cpu_target_celsius == 65.0
        assert config.min_speed_percent == 15
        assert config.max_speed_percent == 100

    def test_custom_values(self) -> None:
        config = Config(gpu_target_celsius=70.0, cpu_target_celsius=60.0)
        assert config.gpu_target_celsius == 70.0
        assert config.cpu_target_celsius == 60.0


class TestPIDController:
    def test_init(self) -> None:
        pid = PIDController(kp=1.0, ki=0.1, kd=0.5, output_min=0, output_max=100)
        assert pid.kp == 1.0
        assert pid.ki == 0.1
        assert pid.kd == 0.5
        assert pid.integral == 0.0

    def test_proportional_only(self) -> None:
        pid = PIDController(kp=2.0, ki=0.0, kd=0.0, output_min=0, output_max=100)
        output = pid.update(error=10.0, dt=1.0)
        assert output == 20.0

    def test_output_clamping_max(self) -> None:
        pid = PIDController(kp=10.0, ki=0.0, kd=0.0, output_min=0, output_max=100)
        output = pid.update(error=20.0, dt=1.0)
        assert output == 100.0

    def test_output_clamping_min(self) -> None:
        pid = PIDController(kp=10.0, ki=0.0, kd=0.0, output_min=15, output_max=100)
        output = pid.update(error=-5.0, dt=1.0)
        assert output == 15.0

    def test_integral_accumulation(self) -> None:
        pid = PIDController(kp=0.0, ki=1.0, kd=0.0, output_min=0, output_max=100)
        pid.update(error=5.0, dt=1.0)
        assert pid.integral == 5.0
        pid.update(error=5.0, dt=1.0)
        assert pid.integral == 10.0

    def test_derivative_first_run_zero(self) -> None:
        pid = PIDController(kp=0.0, ki=0.0, kd=1.0, output_min=0, output_max=100)
        output = pid.update(error=10.0, dt=1.0)
        assert output == 0.0
        assert pid.first_run is False

    def test_derivative_after_first_run(self) -> None:
        pid = PIDController(kp=0.0, ki=0.0, kd=1.0, output_min=0, output_max=100)
        pid.update(error=0.0, dt=1.0)
        output = pid.update(error=10.0, dt=1.0)
        assert output == 10.0

    def test_reset(self) -> None:
        pid = PIDController(kp=1.0, ki=1.0, kd=1.0, output_min=0, output_max=100)
        pid.update(error=10.0, dt=1.0)
        pid.reset()
        assert pid.integral == 0.0
        assert pid.prev_error == 0.0
        assert pid.first_run is True

    def test_anti_windup(self) -> None:
        pid = PIDController(kp=0.0, ki=1.0, kd=0.0, output_min=0, output_max=100)
        for _ in range(1000):
            pid.update(error=100.0, dt=1.0)
        max_integral = (100 - 0) / (1.0 + 1e-6)
        assert pid.integral <= max_integral


class TestFanDaemon:
    @pytest.fixture
    def config(self) -> Config:
        return Config(
            gpu_target_celsius=75.0,
            cpu_target_celsius=65.0,
            min_speed_percent=15,
            max_speed_percent=100,
            interval_seconds=1.0,
        )

    @pytest.fixture
    def hardware(self) -> MockHardware:
        return MockHardware()

    @pytest.fixture
    def daemon(self, config: Config, hardware: MockHardware) -> FanDaemon:
        return FanDaemon(config, hardware)

    def test_init(self, daemon: FanDaemon) -> None:
        assert daemon.running is False
        assert daemon.last_time is None

    def test_read_all_temps_success(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        temps = daemon.read_all_temps()
        assert temps is not None
        assert temps.cpu_celsius == 50.0
        assert temps.gpus_celsius == [70.0, 75.0]
        assert temps.gpu_max_celsius == 75.0

    def test_read_all_temps_cpu_failure(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.cpu_temp = None
        temps = daemon.read_all_temps()
        assert temps is None

    def test_read_all_temps_no_gpus(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.gpu_temps = []
        temps = daemon.read_all_temps()
        assert temps is not None
        assert temps.gpus_celsius == []
        assert temps.gpu_max_celsius is None

    def test_control_loop_sets_speeds(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        daemon.control_loop()
        assert 0 in hardware.zone_speeds
        assert 1 in hardware.zone_speeds
        assert 15 <= hardware.zone_speeds[0] <= 100
        assert 15 <= hardware.zone_speeds[1] <= 100

    def test_control_loop_temp_failure_goes_full(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.cpu_temp = None
        daemon.control_loop()
        assert hardware.full_speed_called

    def test_control_loop_hot_gpu_increases_speed(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.gpu_temps = [40.0, 45.0]
        hardware.cpu_temp = 40.0
        daemon.control_loop()
        cool_zone1 = hardware.zone_speeds[1]

        daemon.pid_zone0.reset()
        daemon.pid_zone1.reset()
        daemon.last_time = None

        hardware.gpu_temps = [85.0, 90.0]
        daemon.control_loop()
        hot_zone1 = hardware.zone_speeds[1]

        assert hot_zone1 > cool_zone1

    def test_control_loop_no_gpus(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.gpu_temps = []
        daemon.control_loop()
        assert 0 in hardware.zone_speeds
        assert 1 in hardware.zone_speeds


class TestMockHardware:
    def test_implements_interface(self) -> None:
        hw = MockHardware()
        assert isinstance(hw, HardwareInterface)

    def test_read_temps(self) -> None:
        hw = MockHardware()
        assert hw.read_cpu_temp() == 50.0
        assert hw.read_gpu_temps() == [70.0, 75.0]

    def test_set_zone_speed(self) -> None:
        hw = MockHardware()
        assert hw.set_zone_speed(0, 50)
        assert hw.zone_speeds[0] == 50

    def test_set_full_speed(self) -> None:
        hw = MockHardware()
        hw.set_full_speed()
        assert hw.full_speed_called
        assert hw.zone_speeds[0] == 100
        assert hw.zone_speeds[1] == 100

    def test_detect_gpus(self) -> None:
        hw = MockHardware()
        count, source = hw.detect_gpus()
        assert count == 2
        assert source == "mock"


def _run_tests(test_file: str) -> None:
    """Run pytest on this file."""
    sys.exit(
        pytest.main([
            test_file,
            "-v",
            "-s",
            "-W", "ignore::pytest.PytestAssertRewriteWarning",
        ])
    )


if __name__ == "__main__":
    _run_tests(__file__)

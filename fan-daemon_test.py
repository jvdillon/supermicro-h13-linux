"""Unit tests for fan-daemon.py."""

import pytest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

_spec = spec_from_loader("fan_daemon", SourceFileLoader("fan_daemon", "fan-daemon.py"))
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)

Config = _module.Config
FanDaemon = _module.FanDaemon
HardwareInterface = _module.HardwareInterface
linear_map = _module.linear_map


class MockHardware(HardwareInterface):
    """Mock hardware for testing."""

    def __init__(self) -> None:
        self.cpu_temp: float | None = 50.0
        self.gpu_temps: list[float] | None = [70.0, 75.0]
        self.hdd_temps: list[float] = []
        self.nvme_temps: list[float] = []
        self.zone_speeds: dict[int, int] = {}
        self.full_speed_called = False

    def read_cpu_temp(self) -> float | None:
        return self.cpu_temp

    def read_gpu_temps(self) -> list[float] | None:
        return self.gpu_temps

    def read_hdd_temps(self) -> list[float]:
        return self.hdd_temps

    def read_nvme_temps(self) -> list[float]:
        return self.nvme_temps

    def set_zone_speed(self, zone: int, percent: int) -> bool:
        self.zone_speeds[zone] = percent
        return True

    def set_full_speed(self) -> bool:
        self.full_speed_called = True
        self.zone_speeds[0] = 100
        self.zone_speeds[1] = 100
        return True

    def detect_gpus(self) -> tuple[int, str]:
        if self.gpu_temps is None:
            return 0, "none"
        return len(self.gpu_temps), "mock"


class TestConfig:
    def test_defaults(self) -> None:
        config = Config()
        assert config.temp_min_celsius == 40.0
        assert config.temp_max_celsius == 80.0
        assert config.min_speed_percent == 15
        assert config.max_speed_percent == 100
        assert config.sensitivity_celsius == 2.0
        assert config.hdd_devices == ()
        assert config.nvme_devices == ()

    def test_custom_values(self) -> None:
        config = Config(
            temp_min_celsius=35.0,
            temp_max_celsius=85.0,
            sensitivity_celsius=3.0,
            hdd_devices=("/dev/sda",),
            nvme_devices=("/dev/nvme0",),
        )
        assert config.temp_min_celsius == 35.0
        assert config.temp_max_celsius == 85.0
        assert config.sensitivity_celsius == 3.0
        assert config.hdd_devices == ("/dev/sda",)
        assert config.nvme_devices == ("/dev/nvme0",)


class TestLinearMap:
    def test_below_min(self) -> None:
        assert linear_map(30, 40, 80, 15, 100) == 15

    def test_above_max(self) -> None:
        assert linear_map(90, 40, 80, 15, 100) == 100

    def test_at_min(self) -> None:
        assert linear_map(40, 40, 80, 15, 100) == 15

    def test_at_max(self) -> None:
        assert linear_map(80, 40, 80, 15, 100) == 100

    def test_midpoint(self) -> None:
        result = linear_map(60, 40, 80, 15, 100)
        assert result == pytest.approx(57.5)

    def test_custom_output_range(self) -> None:
        assert linear_map(30, 40, 85, 20, 100) == 20
        assert linear_map(85, 40, 85, 20, 100) == 100


class TestFanDaemon:
    @pytest.fixture
    def config(self) -> Config:
        return Config(
            temp_min_celsius=40.0,
            temp_max_celsius=80.0,
            min_speed_percent=15,
            max_speed_percent=100,
            speed_step_percent=10,
            sensitivity_celsius=2.0,
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
        assert daemon.last_temp is None
        assert daemon.last_speed is None

    def test_read_all_temps_success(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        temps = daemon.read_all_temps()
        assert temps is not None
        assert temps.cpu_celsius == 50.0
        assert temps.gpus_celsius == [70.0, 75.0]
        assert temps.hdds_celsius == []
        assert temps.nvmes_celsius == []
        assert temps.max_celsius == 75.0

    def test_read_all_temps_with_hdd_nvme(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.hdd_temps = [35.0, 40.0]
        hardware.nvme_temps = [45.0]
        temps = daemon.read_all_temps()
        assert temps is not None
        assert temps.hdds_celsius == [35.0, 40.0]
        assert temps.nvmes_celsius == [45.0]
        assert temps.max_celsius == 75.0  # GPU still hottest

    def test_read_all_temps_hdd_hottest(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.gpu_temps = [40.0]
        hardware.hdd_temps = [55.0]
        hardware.cpu_temp = 45.0
        temps = daemon.read_all_temps()
        assert temps is not None
        assert temps.max_celsius == 55.0  # HDD hottest

    def test_read_all_temps_cpu_failure(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.cpu_temp = None
        temps = daemon.read_all_temps()
        assert temps is None

    def test_read_all_temps_no_gpus(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.gpu_temps = []
        temps = daemon.read_all_temps()
        assert temps is not None
        assert temps.gpus_celsius == []
        assert temps.max_celsius == 50.0  # CPU only

    def test_control_loop_sets_speeds(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        daemon.control_loop()
        assert 0 in hardware.zone_speeds
        assert 1 in hardware.zone_speeds
        assert hardware.zone_speeds[0] == hardware.zone_speeds[1]  # Same speed
        assert 15 <= hardware.zone_speeds[0] <= 100

    def test_control_loop_temp_failure_goes_full(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.cpu_temp = None
        daemon.control_loop()
        assert hardware.full_speed_called

    def test_control_loop_hot_increases_speed(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.gpu_temps = [40.0]
        hardware.cpu_temp = 40.0
        daemon.control_loop()
        cool_speed = hardware.zone_speeds[0]

        hardware.gpu_temps = [85.0]
        daemon.control_loop()
        hot_speed = hardware.zone_speeds[0]

        assert hot_speed > cool_speed

    def test_control_loop_no_gpus(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.gpu_temps = []
        daemon.control_loop()
        assert 0 in hardware.zone_speeds
        assert 1 in hardware.zone_speeds

    def test_sensitivity_skips_small_changes(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.cpu_temp = 60.0
        hardware.gpu_temps = []
        daemon.control_loop()
        first_speed = hardware.zone_speeds[0]
        call_count = len(hardware.zone_speeds)

        # Small temp change within sensitivity
        hardware.cpu_temp = 61.0
        hardware.zone_speeds.clear()
        daemon.control_loop()

        # Should not have set speeds (sensitivity threshold not exceeded)
        assert len(hardware.zone_speeds) == 0

    def test_sensitivity_allows_large_changes(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.cpu_temp = 60.0
        hardware.gpu_temps = []
        daemon.control_loop()

        # Large temp change exceeds sensitivity
        hardware.cpu_temp = 65.0
        hardware.zone_speeds.clear()
        daemon.control_loop()

        # Should have set new speeds
        assert 0 in hardware.zone_speeds

    def test_quantize_speed(self, daemon: FanDaemon) -> None:
        assert daemon._quantize_speed(47.3) == 50
        assert daemon._quantize_speed(42.1) == 40
        assert daemon._quantize_speed(15.0) == 20
        assert daemon._quantize_speed(100.0) == 100


class TestMockHardware:
    def test_implements_interface(self) -> None:
        hw = MockHardware()
        assert isinstance(hw, HardwareInterface)

    def test_read_temps(self) -> None:
        hw = MockHardware()
        assert hw.read_cpu_temp() == 50.0
        assert hw.read_gpu_temps() == [70.0, 75.0]
        assert hw.read_hdd_temps() == []
        assert hw.read_nvme_temps() == []

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

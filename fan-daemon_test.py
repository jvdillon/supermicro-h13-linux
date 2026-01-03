"""Unit tests for fan-daemon.py."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAny=false

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

import pytest

_spec = spec_from_loader("fan_daemon", SourceFileLoader("fan_daemon", "fan-daemon.py"))
assert _spec is not None
_module = module_from_spec(_spec)
sys.modules["fan_daemon"] = _module
assert _spec.loader is not None
_spec.loader.exec_module(_module)

Config = _module.Config
Mappings = _module.Mappings
FanDaemon = _module.FanDaemon
Temps = _module.Temps
ZoneConfig = _module.ZoneConfig


class MockHardware:
    """Mock hardware for testing."""

    config: Config
    cpu_temps: list[float] | None
    gpu_temps: list[float] | None
    ram_temps: list[float] | None
    hdd_temps: list[float] | None
    nvme_temps: list[float] | None
    zone_speeds: dict[int, int]
    full_speed_called: bool

    def __init__(self, config: Config) -> None:
        self.config = config
        self.cpu_temps = [50.0]
        self.gpu_temps = [70.0, 75.0]
        self.ram_temps = []
        self.hdd_temps = []
        self.nvme_temps = []
        self.zone_speeds = {}
        self.full_speed_called = False

    def get_cpu_temps(self) -> list[float] | None:
        return self.cpu_temps

    def get_gpu_temps(self) -> list[float] | None:
        return self.gpu_temps

    def get_ram_temps(self) -> list[float] | None:
        return self.ram_temps

    def get_hdd_temps(self) -> list[float] | None:
        return self.hdd_temps

    def get_nvme_temps(self) -> list[float] | None:
        return self.nvme_temps

    def set_zone_speed(self, zone: int, percent: int) -> bool:
        self.zone_speeds[zone] = percent
        return True

    def set_full_speed(self) -> bool:
        self.full_speed_called = True
        for z in self.config.zones:
            self.zone_speeds[z] = 100
        return True

    def detect_gpus(self) -> int:
        return len(self.gpu_temps) if self.gpu_temps else 0


class TestMappingsParse:
    def test_basic(self) -> None:
        mapping = Mappings.parse("40:15,60:30,80:100")
        assert mapping == ((40.0, 15.0), (60.0, 30.0), (80.0, 100.0))

    def test_sorts_by_temp(self) -> None:
        mapping = Mappings.parse("80:100,40:15,60:30")
        assert mapping == ((40.0, 15.0), (60.0, 30.0), (80.0, 100.0))

    def test_empty_returns_none(self) -> None:
        assert Mappings.parse("") is None

    def test_too_few_points(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            Mappings.parse("40:15")

    def test_invalid_speed(self) -> None:
        with pytest.raises(ValueError, match="Speed must be 0-100"):
            Mappings.parse("40:15,80:150")


class TestMappingsParseSpec:
    def test_gpu_zone(self) -> None:
        key, mapping = Mappings.parse_spec("gpu-zone=40:15,80:100")
        assert key == ("gpu", -1, -1)
        assert mapping == ((40.0, 15.0), (80.0, 100.0))

    def test_gpu_zone0(self) -> None:
        key, _ = Mappings.parse_spec("gpu-zone0=40:15,80:100")
        assert key == ("gpu", -1, 0)

    def test_gpu0_zone1(self) -> None:
        key, _ = Mappings.parse_spec("gpu0-zone1=40:15,80:100")
        assert key == ("gpu", 0, 1)

    def test_disabled(self) -> None:
        key, mapping = Mappings.parse_spec("hdd-zone=")
        assert key == ("hdd", -1, -1)
        assert mapping is None


class TestMappingsGet:
    def test_exact_match(self) -> None:
        m = Mappings({("gpu", 0, 1): ((50.0, 20.0), (80.0, 100.0))})
        assert m.get("gpu", 0, 1) == ((50.0, 20.0), (80.0, 100.0))

    def test_fallback_to_all_zones(self) -> None:
        m = Mappings({("gpu", 0, -1): ((50.0, 20.0), (80.0, 100.0))})
        assert m.get("gpu", 0, 1) == ((50.0, 20.0), (80.0, 100.0))

    def test_fallback_to_all_devices(self) -> None:
        m = Mappings({("gpu", -1, 1): ((50.0, 20.0), (80.0, 100.0))})
        assert m.get("gpu", 0, 1) == ((50.0, 20.0), (80.0, 100.0))

    def test_default_gpu(self) -> None:
        m = Mappings()
        assert m.get("gpu", 0, 0) is not None
        assert m.get("gpu", 0, 1) is not None

    def test_default_cpu_zone0_only(self) -> None:
        m = Mappings()
        assert m.get("cpu", 0, 0) is not None
        assert m.get("cpu", 0, 1) is None


class TestMappingsLookup:
    def test_below_min(self) -> None:
        mapping = ((40.0, 15.0), (80.0, 100.0))
        assert Mappings.lookup(30, mapping) == 15.0

    def test_above_max(self) -> None:
        mapping = ((40.0, 15.0), (80.0, 100.0))
        assert Mappings.lookup(90, mapping) == 100.0

    def test_between_thresholds(self) -> None:
        mapping = ((40.0, 15.0), (80.0, 100.0))
        assert (
            Mappings.lookup(60, mapping) == 15.0
        )  # piecewise constant: 60 >= 40, < 80


class TestFanDaemon:
    @pytest.fixture
    def config(self) -> Config:
        return Config(
            mappings=Mappings(
                {
                    ("cpu", -1, -1): ((40.0, 15.0), (80.0, 100.0)),
                    ("gpu", -1, -1): ((40.0, 15.0), (80.0, 100.0)),
                    ("ram", -1, -1): ((40.0, 15.0), (80.0, 100.0)),
                    ("hdd", -1, -1): ((25.0, 15.0), (50.0, 100.0)),
                    ("nvme", -1, -1): ((35.0, 15.0), (70.0, 100.0)),
                }
            ),
            zones={
                0: ZoneConfig(speed_step_percent=10),
                1: ZoneConfig(speed_step_percent=10),
            },
            sensitivity_celsius=2.0,
        )

    @pytest.fixture
    def hardware(self, config: Config) -> MockHardware:
        return MockHardware(config)

    @pytest.fixture
    def daemon(self, config: Config, hardware: MockHardware) -> FanDaemon:
        return FanDaemon(config, hardware)

    def test_get_all_temps(self, daemon: FanDaemon) -> None:
        temps = daemon.get_all_temps()
        assert temps is not None
        assert temps.cpus_celsius == [50.0]

    def test_get_all_temps_failure(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        hardware.cpu_temps = None
        assert daemon.get_all_temps() is None

    def test_compute_zone_speeds(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        hardware.cpu_temps = [70.0]
        hardware.gpu_temps = [40.0]
        temps = daemon.get_all_temps()
        assert temps is not None
        speeds = daemon.compute_zone_speeds(temps)
        assert speeds[0][1] == "CPU0"

    def test_control_loop(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        daemon.control_loop()
        assert 0 in hardware.zone_speeds

    def test_control_loop_failure_goes_full(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        hardware.cpu_temps = None
        daemon.control_loop()
        assert hardware.full_speed_called

    def test_sensitivity(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        hardware.cpu_temps = [60.0]
        hardware.gpu_temps = []
        daemon.control_loop()
        hardware.cpu_temps = [61.0]
        hardware.zone_speeds.clear()
        daemon.control_loop()
        assert len(hardware.zone_speeds) == 0  # skipped due to sensitivity

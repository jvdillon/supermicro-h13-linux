"""Unit tests for fan-daemon.py."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAny=false

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from unittest.mock import patch

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
Supermicro = _module.Supermicro
_run_cmd = _module._run_cmd


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
        assert mapping == ((40.0, 15.0, 0.0), (60.0, 30.0, 0.0), (80.0, 100.0, 0.0))

    def test_sorts_by_temp(self) -> None:
        mapping = Mappings.parse("80:100,40:15,60:30")
        assert mapping == ((40.0, 15.0, 0.0), (60.0, 30.0, 0.0), (80.0, 100.0, 0.0))

    def test_with_hysteresis(self) -> None:
        mapping = Mappings.parse("40:15:3,80:100:5")
        assert mapping == ((40.0, 15.0, 3.0), (80.0, 100.0, 5.0))

    def test_empty_returns_none(self) -> None:
        assert Mappings.parse("") is None

    def test_too_few_points(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            Mappings.parse("40:15")

    def test_invalid_speed(self) -> None:
        with pytest.raises(ValueError, match="Speed must be 0-100"):
            Mappings.parse("40:15,80:150")

    def test_invalid_hysteresis(self) -> None:
        with pytest.raises(ValueError, match="Hysteresis must be >= 0"):
            Mappings.parse("40:15:-5,80:100:5")

    def test_invalid_format(self) -> None:
        with pytest.raises(ValueError, match="Invalid point format"):
            Mappings.parse("40:15:5:extra,80:100")


class TestMappingsParseSpec:
    def test_gpu_zone(self) -> None:
        key, mapping = Mappings.parse_spec("gpu-zone=40:15,80:100")
        assert key == ("gpu", -1, -1)
        assert mapping == ((40.0, 15.0, 0.0), (80.0, 100.0, 0.0))

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

    def test_missing_equals(self) -> None:
        with pytest.raises(ValueError, match="missing '='"):
            Mappings.parse_spec("gpu-zone40:15,80:100")

    def test_invalid_key_format(self) -> None:
        with pytest.raises(ValueError, match="Invalid mapping key format"):
            Mappings.parse_spec("badkey=40:15,80:100")

    def test_ram_zone(self) -> None:
        key, _ = Mappings.parse_spec("ram-zone0=40:15,80:100")
        assert key == ("ram", -1, 0)


class TestMappingsGet:
    def test_exact_match(self) -> None:
        m = Mappings({("gpu", 0, 1): ((50.0, 20.0, 0.0), (80.0, 100.0, 0.0))})
        assert m.get("gpu", 0, 1) == ((50.0, 20.0, 0.0), (80.0, 100.0, 0.0))

    def test_fallback_to_all_zones(self) -> None:
        m = Mappings({("gpu", 0, -1): ((50.0, 20.0, 0.0), (80.0, 100.0, 0.0))})
        assert m.get("gpu", 0, 1) == ((50.0, 20.0, 0.0), (80.0, 100.0, 0.0))

    def test_fallback_to_all_devices(self) -> None:
        m = Mappings({("gpu", -1, 1): ((50.0, 20.0, 0.0), (80.0, 100.0, 0.0))})
        assert m.get("gpu", 0, 1) == ((50.0, 20.0, 0.0), (80.0, 100.0, 0.0))

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
        mapping = ((40.0, 15.0, 0.0), (80.0, 100.0, 0.0))
        speed, thresh = Mappings.lookup(30, mapping)
        assert speed == 15.0
        assert thresh == 40.0  # returns first threshold

    def test_above_max(self) -> None:
        mapping = ((40.0, 15.0, 0.0), (80.0, 100.0, 0.0))
        speed, thresh = Mappings.lookup(90, mapping)
        assert speed == 100.0
        assert thresh == 80.0

    def test_between_thresholds(self) -> None:
        mapping = ((40.0, 15.0, 0.0), (80.0, 100.0, 0.0))
        speed, thresh = Mappings.lookup(60, mapping)
        assert speed == 15.0  # piecewise constant: 60 >= 40, < 80
        assert thresh == 40.0

    def test_hysteresis_rising(self) -> None:
        mapping = ((40.0, 15.0, 5.0), (70.0, 50.0, 5.0), (80.0, 100.0, 5.0))
        # Rising from below - active at 40, now at 75 -> should go to 70 threshold
        speed, thresh = Mappings.lookup(75, mapping, active_threshold=40.0)
        assert speed == 50.0
        assert thresh == 70.0

    def test_hysteresis_falling_stays(self) -> None:
        mapping = ((40.0, 15.0, 5.0), (70.0, 50.0, 5.0), (80.0, 100.0, 5.0))
        # Falling from 80 to 68 - should stay at 70 threshold (68 >= 70-5=65)
        speed, thresh = Mappings.lookup(68, mapping, active_threshold=70.0)
        assert speed == 50.0
        assert thresh == 70.0

    def test_hysteresis_falling_drops(self) -> None:
        mapping = ((40.0, 15.0, 5.0), (70.0, 50.0, 5.0), (80.0, 100.0, 5.0))
        # Falling from 70 to 64 - should drop to 40 threshold (64 < 70-5=65)
        speed, thresh = Mappings.lookup(64, mapping, active_threshold=70.0)
        assert speed == 15.0
        assert thresh == 40.0


class TestFanDaemon:
    @pytest.fixture
    def config(self) -> Config:
        return Config(
            mappings=Mappings(
                {
                    ("cpu", -1, -1): ((40.0, 15.0, 0.0), (80.0, 100.0, 0.0)),
                    ("gpu", -1, -1): ((40.0, 15.0, 0.0), (80.0, 100.0, 0.0)),
                    ("ram", -1, -1): ((40.0, 15.0, 0.0), (80.0, 100.0, 0.0)),
                    ("hdd", -1, -1): ((25.0, 15.0, 0.0), (50.0, 100.0, 0.0)),
                    ("nvme", -1, -1): ((35.0, 15.0, 0.0), (70.0, 100.0, 0.0)),
                }
            ),
            zones={
                0: ZoneConfig(speed_step_percent=10),
                1: ZoneConfig(speed_step_percent=10),
            },
            hysteresis_celsius=5.0,
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

    def test_hysteresis(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        # Set up mapping with clear thresholds - use zone 0 specific keys to override defaults
        daemon.config = Config(
            mappings=Mappings(
                {
                    ("cpu", -1, 0): ((40.0, 15.0, 5.0), (70.0, 50.0, 5.0)),
                    ("gpu", -1, 0): ((40.0, 15.0, 5.0), (70.0, 50.0, 5.0)),
                    ("ram", -1, 0): ((40.0, 15.0, 5.0), (70.0, 50.0, 5.0)),
                    ("hdd", -1, 0): ((25.0, 15.0, 5.0), (50.0, 100.0, 5.0)),
                    ("nvme", -1, 0): ((35.0, 15.0, 5.0), (70.0, 100.0, 5.0)),
                }
            ),
            zones={
                0: ZoneConfig(speed_step_percent=10),
                1: ZoneConfig(speed_step_percent=10),
            },
            hysteresis_celsius=5.0,
        )
        hardware.cpu_temps = [75.0]
        hardware.gpu_temps = []
        daemon.control_loop()
        # Should be at 70 threshold -> 50%
        assert hardware.zone_speeds[0] == 50

        # Drop to 68 - should stay at 50% due to hysteresis (68 >= 70-5=65)
        hardware.cpu_temps = [68.0]
        daemon.control_loop()
        assert hardware.zone_speeds[0] == 50

        # Drop to 64 - should drop to 15% (64 < 65)
        hardware.cpu_temps = [64.0]
        daemon.control_loop()
        assert hardware.zone_speeds[0] == 20  # quantized to step

    def test_get_all_temps_gpu_failure(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        hardware.gpu_temps = None
        assert daemon.get_all_temps() is None

    def test_get_all_temps_ram_optional(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        hardware.ram_temps = None
        temps = daemon.get_all_temps()
        assert temps is not None
        assert temps.rams_celsius == []

    def test_get_all_temps_hdd_optional(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        hardware.hdd_temps = None
        temps = daemon.get_all_temps()
        assert temps is not None
        assert temps.hdds_celsius == []

    def test_get_all_temps_nvme_optional(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        hardware.nvme_temps = None
        temps = daemon.get_all_temps()
        assert temps is not None
        assert temps.nvmes_celsius == []

    def test_quantize_speed(self, daemon: FanDaemon) -> None:
        assert daemon._quantize_speed(15, 10) == 20  # round(1.5) = 2 (banker's)
        assert daemon._quantize_speed(14, 10) == 10
        assert daemon._quantize_speed(26, 10) == 30  # round(2.6) = 3
        assert daemon._quantize_speed(50, 10) == 50

    def test_control_loop_set_zone_speed_failure(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        # Make set_zone_speed fail
        def fail_set_zone_speed(zone: int, percent: int) -> bool:  # noqa: ARG001
            del zone, percent
            return False

        hardware.set_zone_speed = fail_set_zone_speed  # type: ignore[method-assign]
        daemon.control_loop()
        assert hardware.full_speed_called

    def test_compute_zone_speeds_no_candidates(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        # Use empty mappings
        daemon.config = Config(
            mappings=Mappings({}),
            zones={0: ZoneConfig()},
        )
        hardware.cpu_temps = []
        hardware.gpu_temps = []
        temps = daemon.get_all_temps()
        assert temps is not None
        speeds = daemon.compute_zone_speeds(temps)
        assert speeds[0] == (15, "none", 0.0)  # min speed, no trigger


class TestSupermicro:
    """Tests for Supermicro hardware class with mocked _run_cmd."""

    @pytest.fixture
    def config(self) -> Config:
        return Config(
            hdd_devices=(),  # Disable auto-detect
            nvme_devices=(),  # Disable auto-detect
        )

    @pytest.fixture
    def hw(self, config: Config) -> Supermicro:
        with patch.object(_module, "_detect_hdds", return_value=()):
            with patch.object(_module, "_detect_nvmes", return_value=()):
                return Supermicro(config)

    def test_get_cpu_temps_success(self, hw: Supermicro) -> None:
        ipmi_output = """Sensor ID              : CPU Temp (0x1)
 Entity ID             : 3.1 (Processor)
 Sensor Type (Threshold)  : Temperature (0x01)
 Sensor Reading        : 45 (+/- 0) degrees C
 Status                : ok
"""
        with patch.object(_module, "_run_cmd", return_value=ipmi_output):
            temps = hw.get_cpu_temps()
        assert temps == [45.0]

    def test_get_cpu_temps_failure(self, hw: Supermicro) -> None:
        with patch.object(_module, "_run_cmd", return_value=None):
            temps = hw.get_cpu_temps()
        assert temps is None

    def test_get_cpu_temps_parse_failure(self, hw: Supermicro) -> None:
        with patch.object(_module, "_run_cmd", return_value="garbage output"):
            temps = hw.get_cpu_temps()
        assert temps is None

    def test_get_gpu_temps_nvidia_success(self, hw: Supermicro) -> None:
        nvidia_output = "65\n70\n"
        with patch.object(_module, "_run_cmd", return_value=nvidia_output):
            temps = hw.get_gpu_temps()
        assert temps == [65.0, 70.0]

    def test_get_gpu_temps_nvidia_failure_ipmi_fallback(self, hw: Supermicro) -> None:
        ipmi_output = """Sensor ID              : GPU1 Temp (0x1)
 Sensor Reading        : 72 degrees C
"""

        def mock_run_cmd(cmd: list[str], _timeout: float) -> str | None:
            if "nvidia-smi" in cmd:
                return None
            if "GPU1 Temp" in cmd:
                return ipmi_output
            return None

        with patch.object(_module, "_run_cmd", side_effect=mock_run_cmd):
            temps = hw.get_gpu_temps()
        assert temps == [72.0]

    def test_get_gpu_temps_all_fail(self, hw: Supermicro) -> None:
        with patch.object(_module, "_run_cmd", return_value=None):
            temps = hw.get_gpu_temps()
        assert temps is None

    def test_get_ram_temps_success(self, hw: Supermicro) -> None:
        ipmi_output = """Sensor ID              : DIMMA~F Temp (0x1)
 Sensor Reading        : 38 degrees C
"""
        with patch.object(_module, "_run_cmd", return_value=ipmi_output):
            temps = hw.get_ram_temps()
        assert 38.0 in temps

    def test_get_hdd_temps_success(self) -> None:
        config = Config(hdd_devices=("/dev/sda",), nvme_devices=())
        with patch.object(_module, "_detect_hdds", return_value=()):
            with patch.object(_module, "_detect_nvmes", return_value=()):
                hw = Supermicro(config)
        smartctl_output = """smartctl 7.4 2023-08-01 r5530
=== START OF READ SMART DATA SECTION ===
SMART Attributes Data Structure revision number: 16
Vendor Specific SMART Attributes with Thresholds:
ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
194 Temperature_Celsius     0x0022   100   100   000    Old_age   Always       -       35
"""
        with patch.object(_module, "_run_cmd", return_value=smartctl_output):
            temps = hw.get_hdd_temps()
        assert temps == [35.0]

    def test_get_nvme_temps_success(self) -> None:
        config = Config(hdd_devices=(), nvme_devices=("/dev/nvme0n1",))
        with patch.object(_module, "_detect_hdds", return_value=()):
            with patch.object(_module, "_detect_nvmes", return_value=()):
                hw = Supermicro(config)
        nvme_output = """Smart Log for NVME device:nvme0n1 namespace-id:ffffffff
critical_warning                        : 0
temperature                             : 42 C
"""
        with patch.object(_module, "_run_cmd", return_value=nvme_output):
            temps = hw.get_nvme_temps()
        assert temps == [42.0]

    def test_set_zone_speed_success(self, hw: Supermicro) -> None:
        call_count = 0

        def mock_run_cmd(cmd: list[str], _timeout: float) -> str | None:
            nonlocal call_count
            call_count += 1
            if "0x45" in cmd and "0x00" in cmd:
                return "01"  # Already in full mode
            return ""

        with patch.object(_module, "_run_cmd", side_effect=mock_run_cmd):
            result = hw.set_zone_speed(0, 50)
        assert result is True
        assert hw.current_speeds[0] == 50

    def test_set_zone_speed_cached(self, hw: Supermicro) -> None:
        hw.current_speeds[0] = 50
        call_count = 0

        def mock_run_cmd(_cmd: list[str], _timeout: float) -> str | None:
            nonlocal call_count
            call_count += 1
            return ""

        with patch.object(_module, "_run_cmd", side_effect=mock_run_cmd):
            result = hw.set_zone_speed(0, 50)
        assert result is True
        assert call_count == 0  # No calls because speed is cached

    def test_set_zone_speed_failure(self, hw: Supermicro) -> None:
        def mock_run_cmd(cmd: list[str], _timeout: float) -> str | None:
            if "0x45" in cmd and "0x00" in cmd:
                return "01"  # Already in full mode
            if "0x66" in cmd:
                return None  # Fail the set speed command
            return ""

        with patch.object(_module, "_run_cmd", side_effect=mock_run_cmd):
            result = hw.set_zone_speed(0, 50)
        assert result is False

    def test_set_full_speed(self, hw: Supermicro) -> None:
        def mock_run_cmd(cmd: list[str], _timeout: float) -> str | None:
            if "0x45" in cmd and "0x00" in cmd:
                return "01"
            return ""

        with patch.object(_module, "_run_cmd", side_effect=mock_run_cmd):
            result = hw.set_full_speed()
        assert result is True
        assert hw.current_speeds[0] == 100
        assert hw.current_speeds[1] == 100

    def test_detect_gpus(self, hw: Supermicro) -> None:
        nvidia_output = "65\n70\n"
        with patch.object(_module, "_run_cmd", return_value=nvidia_output):
            count = hw.detect_gpus()
        assert count == 2

    def test_valid_temp_in_range(self, hw: Supermicro) -> None:
        assert hw._valid_temp(50.0) == 50.0

    def test_valid_temp_out_of_range(self, hw: Supermicro) -> None:
        assert hw._valid_temp(-10.0) is None
        assert hw._valid_temp(150.0) is None

    def test_parse_nvidia_temps_invalid(self, hw: Supermicro) -> None:
        assert hw._parse_nvidia_temps("not a number\n") is None

    def test_parse_nvidia_temps_out_of_range(self, hw: Supermicro) -> None:
        assert hw._parse_nvidia_temps("150\n") is None

    def test_ensure_full_mode_needs_set(self, hw: Supermicro) -> None:
        call_sequence: list[str] = []

        def mock_run_cmd(cmd: list[str], _timeout: float) -> str | None:
            call_sequence.append(" ".join(cmd))
            if "0x45" in cmd and "0x00" in cmd:
                return "00"  # Not in full mode
            if "0x45" in cmd and "0x01" in cmd:
                return ""  # Set mode success
            return ""

        def noop_sleep(_seconds: float) -> None:
            pass

        with patch.object(_module, "_run_cmd", side_effect=mock_run_cmd):
            with patch.object(_module, "time") as mock_time:
                mock_time.sleep = noop_sleep  # type: ignore[assignment]
                result = hw._ensure_full_mode()
        assert result is True
        assert any("0x01 0x01" in c for c in call_sequence)

    def test_ensure_full_mode_failure(self, hw: Supermicro) -> None:
        def mock_run_cmd(cmd: list[str], _timeout: float) -> str | None:
            if "0x45" in cmd:
                return None  # All mode commands fail
            return ""

        with patch.object(_module, "_run_cmd", side_effect=mock_run_cmd):
            result = hw._ensure_full_mode()
        assert result is False

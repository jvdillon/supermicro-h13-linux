"""Unit tests for fan-daemon.py."""
# pyright: basic
# ruff: noqa: SLF001

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from typing import final
from unittest.mock import MagicMock, patch

import pytest

_spec = spec_from_loader("fan_daemon", SourceFileLoader("fan_daemon", "fan-daemon.py"))
assert _spec is not None
_module = module_from_spec(_spec)
sys.modules["fan_daemon"] = _module
assert _spec.loader is not None
_spec.loader.exec_module(_module)

FanSpeed = _module.FanSpeed
FanDaemon = _module.FanDaemon
SupermicroH13 = _module.SupermicroH13
run_cmd = _module.run_cmd


def _make_mock_sensors(
    cpu: tuple[float, ...] | None = (45.0,),
    gpu: tuple[float, ...] | None = (65.0, 70.0),
    hdd: tuple[float, ...] | None = None,
    nvme: tuple[float, ...] | None = None,
) -> list[MagicMock]:
    """Create mock sensor list for SupermicroH13 tests."""
    cpu_sensor = MagicMock()
    cpu_sensor.get.return_value = {"cpu": cpu}
    gpu_sensor = MagicMock()
    gpu_sensor.get.return_value = {"gpu": gpu}
    hdd_sensor = MagicMock()
    hdd_sensor.get.return_value = {"hdd": hdd}
    nvme_sensor = MagicMock()
    nvme_sensor.get.return_value = {"nvme": nvme}
    return [cpu_sensor, gpu_sensor, hdd_sensor, nvme_sensor]


class MockHardware:
    """Mock hardware for testing."""

    temps: dict[str, tuple[int, ...] | None] | None
    zones: tuple[int, ...]
    zone_speeds: dict[int, int]
    fail_safe_called: bool

    def __init__(self, zones: tuple[int, ...] = (0, 1)) -> None:
        self.zones = zones
        self.temps = {
            "cpu": (50,),
            "gpu": (70, 75),
            "gpu_ipmi": None,
            "ram": None,
            "hdd": None,
            "nvme": None,
            "vrm_soc": None,
            "vrm_cpu": None,
            "vrm_vddio": None,
            "system": None,
            "peripheral": None,
        }
        self.zone_speeds = {}
        self.fail_safe_called = False

    def initialize(self) -> bool:
        return True

    def get_temps(self) -> dict[str, tuple[int, ...] | None] | None:
        return self.temps

    def get_zones(self) -> tuple[int, ...]:
        return self.zones

    def set_zone_speed(self, zone: int, percent: int) -> bool:
        self.zone_speeds[zone] = percent
        return True

    def set_fail_safe(self) -> bool:
        self.fail_safe_called = True
        return True


class TestFanSpeedConfigParse:
    def test_basic(self) -> None:
        _, mapping = FanSpeed.Config._parse_speeds("x=40:15,60:30,80:100")
        assert mapping == (
            (40.0, 15.0, None, None),
            (60.0, 30.0, None, None),
            (80.0, 100.0, None, None),
        )

    def test_sorts_by_temp(self) -> None:
        _, mapping = FanSpeed.Config._parse_speeds("x=80:100,40:15,60:30")
        assert mapping == (
            (40.0, 15.0, None, None),
            (60.0, 30.0, None, None),
            (80.0, 100.0, None, None),
        )

    def test_with_hysteresis_celsius(self) -> None:
        _, mapping = FanSpeed.Config._parse_speeds("x=40:15:3,80:100:5")
        assert mapping == ((40.0, 15.0, 3.0, None), (80.0, 100.0, 5.0, None))

    def test_with_hysteresis_full(self) -> None:
        _, mapping = FanSpeed.Config._parse_speeds("x=40:15:3:20,80:100:5:60")
        assert mapping == ((40.0, 15.0, 3.0, 20.0), (80.0, 100.0, 5.0, 60.0))

    def test_with_hysteresis_seconds_empty_celsius(self) -> None:
        # Empty hyst_c placeholder: 70:80::60 means default temp hyst, 60s time hyst
        _, mapping = FanSpeed.Config._parse_speeds("x=40:15::30,80:100::60")
        assert mapping == ((40.0, 15.0, None, 30.0), (80.0, 100.0, None, 60.0))

    def test_empty_returns_none(self) -> None:
        _, mapping = FanSpeed.Config._parse_speeds("x=")
        assert mapping is None

    def test_too_few_points(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            FanSpeed.Config._parse_speeds("x=40:15")

    def test_invalid_speed(self) -> None:
        with pytest.raises(ValueError, match="Speed must be 0-100"):
            FanSpeed.Config._parse_speeds("x=40:15,80:150")

    def test_invalid_hysteresis_celsius(self) -> None:
        with pytest.raises(ValueError, match="Hysteresis celsius must be >= 0"):
            FanSpeed.Config._parse_speeds("x=40:15:-5,80:100:5")

    def test_invalid_hysteresis_seconds(self) -> None:
        with pytest.raises(ValueError, match="Hysteresis seconds must be >= 0"):
            FanSpeed.Config._parse_speeds("x=40:15:5:-30,80:100:5:60")

    def test_invalid_format(self) -> None:
        with pytest.raises(ValueError, match="Invalid point format"):
            FanSpeed.Config._parse_speeds("x=40:15:5:10:extra,80:100")

    def test_gpu_zone(self) -> None:
        key, mapping = FanSpeed.Config._parse_speeds("gpu-zone=40:15,80:100")
        assert key == ("gpu", -1, -1)
        assert mapping == ((40.0, 15.0, None, None), (80.0, 100.0, None, None))

    def test_gpu_zone0(self) -> None:
        key, _ = FanSpeed.Config._parse_speeds("gpu-zone0=40:15,80:100")
        assert key == ("gpu", -1, 0)

    def test_gpu0_zone1(self) -> None:
        key, _ = FanSpeed.Config._parse_speeds("gpu0-zone1=40:15,80:100")
        assert key == ("gpu", 0, 1)

    def test_disabled(self) -> None:
        key, mapping = FanSpeed.Config._parse_speeds("hdd-zone=")
        assert key == ("hdd", -1, -1)
        assert mapping is None

    def test_missing_equals(self) -> None:
        with pytest.raises(ValueError, match="missing '='"):
            FanSpeed.Config._parse_speeds("gpu-zone40:15,80:100")

    def test_invalid_key_format(self) -> None:
        with pytest.raises(ValueError, match="Invalid mapping key format"):
            FanSpeed.Config._parse_speeds("123gpu=40:15,80:100")  # starts with digit

    def test_ram_zone(self) -> None:
        key, _ = FanSpeed.Config._parse_speeds("ram-zone0=40:15,80:100")
        assert key == ("ram", -1, 0)


class TestFanSpeedGet:
    def test_exact_match(self) -> None:
        m = FanSpeed.Config(
            speeds={
                ("gpu", 0, 1): ((50.0, 20.0, None, None), (80.0, 100.0, None, None))
            }
        ).setup()
        result = m.get("gpu", 0, 1)
        assert result is not None
        mapping, key = result
        assert mapping == ((50.0, 20.0, None, None), (80.0, 100.0, None, None))
        assert key == ("gpu", 0, 1)

    def test_fallback_to_all_zones(self) -> None:
        m = FanSpeed.Config(
            speeds={
                ("gpu", 0, -1): ((50.0, 20.0, None, None), (80.0, 100.0, None, None))
            }
        ).setup()
        result = m.get("gpu", 0, 1)
        assert result is not None
        mapping, key = result
        assert mapping == ((50.0, 20.0, None, None), (80.0, 100.0, None, None))
        assert key == ("gpu", 0, -1)  # matched wildcard zone

    def test_fallback_to_all_devices(self) -> None:
        m = FanSpeed.Config(
            speeds={
                ("gpu", -1, 1): ((50.0, 20.0, None, None), (80.0, 100.0, None, None))
            }
        ).setup()
        result = m.get("gpu", 0, 1)
        assert result is not None
        mapping, key = result
        assert mapping == ((50.0, 20.0, None, None), (80.0, 100.0, None, None))
        assert key == ("gpu", -1, 1)  # matched wildcard device

    def test_default_gpu(self) -> None:
        m = FanSpeed.Config().setup()
        assert m.get("gpu", 0, 0) is not None
        assert m.get("gpu", 0, 1) is not None

    def test_default_cpu_zone0_only(self) -> None:
        m = FanSpeed.Config().setup()
        assert m.get("cpu", 0, 0) is not None
        assert m.get("cpu", 0, 1) is None


class TestFanSpeedLookup:
    @pytest.fixture
    def m(self) -> FanSpeed:
        return FanSpeed.Config(hysteresis_celsius=5.0, hysteresis_seconds=0.0).setup()

    def test_below_min(self, m: FanSpeed) -> None:
        mapping = ((40.0, 15.0, None, None), (80.0, 100.0, None, None))
        speed, thresh, drop_time = m.lookup(30, mapping)
        assert speed == 15.0
        assert thresh == 40.0  # returns first threshold
        assert drop_time is None

    def test_above_max(self, m: FanSpeed) -> None:
        mapping = ((40.0, 15.0, None, None), (80.0, 100.0, None, None))
        speed, thresh, drop_time = m.lookup(90, mapping)
        assert speed == 100.0
        assert thresh == 80.0
        assert drop_time is None

    def test_between_thresholds(self, m: FanSpeed) -> None:
        mapping = ((40.0, 15.0, None, None), (80.0, 100.0, None, None))
        speed, thresh, drop_time = m.lookup(60, mapping)
        assert speed == 15.0  # piecewise constant: 60 >= 40, < 80
        assert thresh == 40.0
        assert drop_time is None

    def test_hysteresis_rising(self, m: FanSpeed) -> None:
        mapping = (
            (40.0, 15.0, 5.0, None),
            (70.0, 50.0, 5.0, None),
            (80.0, 100.0, 5.0, None),
        )
        # Rising from below - active at 40, now at 75 -> should go to 70 threshold
        speed, thresh, drop_time = m.lookup(75, mapping, active_threshold=40.0)
        assert speed == 50.0
        assert thresh == 70.0
        assert drop_time is None

    def test_hysteresis_falling_stays(self, m: FanSpeed) -> None:
        mapping = (
            (40.0, 15.0, 5.0, None),
            (70.0, 50.0, 5.0, None),
            (80.0, 100.0, 5.0, None),
        )
        # Falling from 80 to 68 - should stay at 70 threshold (68 >= 70-5=65)
        speed, thresh, drop_time = m.lookup(68, mapping, active_threshold=70.0)
        assert speed == 50.0
        assert thresh == 70.0
        assert drop_time is None  # Not in drop zone yet

    def test_hysteresis_falling_drops(self, m: FanSpeed) -> None:
        mapping = (
            (40.0, 15.0, 5.0, None),
            (70.0, 50.0, 5.0, None),
            (80.0, 100.0, 5.0, None),
        )
        # Falling from 70 to 64 - should drop to 40 threshold (64 < 70-5=65)
        # With hysteresis_seconds=0, drops immediately
        speed, thresh, drop_time = m.lookup(64, mapping, active_threshold=70.0)
        assert speed == 15.0
        assert thresh == 40.0
        assert drop_time is None

    def test_time_hysteresis_starts_timer(self) -> None:
        """When temp enters drop zone, timer starts and speed stays high."""
        m = FanSpeed(
            FanSpeed.Config(hysteresis_celsius=0.0, hysteresis_seconds=30.0, speeds={})
        )
        mapping = ((40.0, 15.0, 5.0, 30.0), (70.0, 50.0, 5.0, 30.0))
        # Temp 64 < 70-5=65, enters drop zone, but timer just started
        speed, thresh, drop_time = m.lookup(
            64, mapping, active_threshold=70.0, time_in_drop_zone=None, current_time=100
        )
        assert speed == 50.0  # Stay at active speed
        assert thresh == 70.0  # Stay at active threshold
        assert drop_time == 100  # Timer started

    def test_time_hysteresis_waiting(self) -> None:
        """While waiting for time hysteresis, stay at high speed."""
        m = FanSpeed(
            FanSpeed.Config(hysteresis_celsius=0.0, hysteresis_seconds=30.0, speeds={})
        )
        mapping = ((40.0, 15.0, 5.0, 30.0), (70.0, 50.0, 5.0, 30.0))
        # 15 seconds into 30s wait
        speed, thresh, drop_time = m.lookup(
            64, mapping, active_threshold=70.0, time_in_drop_zone=100, current_time=115
        )
        assert speed == 50.0
        assert thresh == 70.0
        assert drop_time == 100  # Timer preserved

    def test_time_hysteresis_expires(self) -> None:
        """When time hysteresis expires, drop to lower speed."""
        m = FanSpeed(
            FanSpeed.Config(hysteresis_celsius=0.0, hysteresis_seconds=30.0, speeds={})
        )
        mapping = ((40.0, 15.0, 5.0, 30.0), (70.0, 50.0, 5.0, 30.0))
        # 30+ seconds elapsed
        speed, thresh, drop_time = m.lookup(
            64, mapping, active_threshold=70.0, time_in_drop_zone=100, current_time=130
        )
        assert speed == 15.0  # Dropped
        assert thresh == 40.0  # New threshold
        assert drop_time is None  # Timer reset

    def test_time_hysteresis_reset_on_spike(self) -> None:
        """Temp spike resets the timer."""
        m = FanSpeed(
            FanSpeed.Config(hysteresis_celsius=0.0, hysteresis_seconds=30.0, speeds={})
        )
        mapping = ((40.0, 15.0, 5.0, 30.0), (70.0, 50.0, 5.0, 30.0))
        # Temp goes back above drop zone threshold (66 >= 65)
        speed, thresh, drop_time = m.lookup(
            66, mapping, active_threshold=70.0, time_in_drop_zone=100, current_time=115
        )
        assert speed == 50.0
        assert thresh == 70.0
        assert drop_time is None  # Timer reset


class TestFanDaemon:
    @pytest.fixture
    def fan_speed(self) -> FanSpeed:
        return FanSpeed.Config(
            speeds={
                ("cpu", -1, -1): (
                    (0.0, 15.0, None, None),
                    (40.0, 15.0, None, None),
                    (80.0, 100.0, None, None),
                ),
                ("gpu", -1, -1): (
                    (0.0, 15.0, None, None),
                    (40.0, 15.0, None, None),
                    (80.0, 100.0, None, None),
                ),
                ("ram", -1, -1): (
                    (0.0, 15.0, None, None),
                    (40.0, 15.0, None, None),
                    (80.0, 100.0, None, None),
                ),
                ("hdd", -1, -1): (
                    (0.0, 15.0, None, None),
                    (25.0, 15.0, None, None),
                    (50.0, 100.0, None, None),
                ),
                ("nvme", -1, -1): (
                    (0.0, 15.0, None, None),
                    (35.0, 15.0, None, None),
                    (70.0, 100.0, None, None),
                ),
            },
            hysteresis_celsius=5.0,
            hysteresis_seconds=0.0,
        ).setup()

    @pytest.fixture
    def hardware(self) -> MockHardware:
        return MockHardware()

    @pytest.fixture
    def daemon(self, fan_speed: FanSpeed, hardware: MockHardware) -> FanDaemon:
        config = FanDaemon.Config()
        return config.setup(hardware, fan_speed)

    def test_get_temps(self, hardware: MockHardware) -> None:
        temps = hardware.get_temps()
        assert temps is not None
        assert temps["cpu"] == (50,)

    def test_get_temps_failure(self, hardware: MockHardware) -> None:
        hardware.temps = None
        assert hardware.get_temps() is None

    def test_compute_zone_speeds(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        assert hardware.temps is not None
        hardware.temps["cpu"] = (70,)
        hardware.temps["gpu"] = (40,)
        temps = hardware.get_temps()
        assert temps is not None
        speeds = daemon._compute_zone_speeds(temps)
        assert speeds[0][1] == "CPU0"

    def test_control_loop(self, daemon: FanDaemon, hardware: MockHardware) -> None:
        daemon.control_loop()
        assert 0 in hardware.zone_speeds

    def test_control_loop_failure_goes_fail_safe(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        hardware.temps = None
        daemon.control_loop()
        assert hardware.fail_safe_called

    def test_hysteresis(self, hardware: MockHardware) -> None:
        # Set up mapping with clear thresholds - use zone 0 specific keys to override defaults
        fan_speed = FanSpeed.Config(
            speeds={
                ("cpu", -1, 0): ((40.0, 15.0, 5.0, None), (70.0, 50.0, 5.0, None)),
                ("gpu", -1, 0): ((40.0, 15.0, 5.0, None), (70.0, 50.0, 5.0, None)),
                ("ram", -1, 0): ((40.0, 15.0, 5.0, None), (70.0, 50.0, 5.0, None)),
                ("hdd", -1, 0): ((25.0, 15.0, 5.0, None), (50.0, 100.0, 5.0, None)),
                ("nvme", -1, 0): ((35.0, 15.0, 5.0, None), (70.0, 100.0, 5.0, None)),
            },
            hysteresis_celsius=5.0,
            hysteresis_seconds=0.0,  # Disable time hysteresis for this test
        ).setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)
        assert hardware.temps is not None
        hardware.temps["cpu"] = (75,)
        hardware.temps["gpu"] = None
        daemon.control_loop()
        # Should be at 70 threshold -> 50%
        assert hardware.zone_speeds[0] == 50

        # Drop to 68 - should stay at 50% due to hysteresis (68 >= 70-5=65)
        hardware.temps["cpu"] = (68,)
        daemon.control_loop()
        assert hardware.zone_speeds[0] == 50

        # Drop to 64 - should drop to 15% (64 < 65)
        hardware.temps["cpu"] = (64,)
        daemon.control_loop()
        assert hardware.zone_speeds[0] == 15

    def test_hysteresis_non_winner_threshold_updated(
        self, hardware: MockHardware
    ) -> None:
        """Non-winning devices must update their active_threshold.

        Bug regression test: Previously, only the winning device's threshold
        was updated, causing non-winners to keep stale thresholds and restart
        their hysteresis timers every iteration.
        """
        fan_speed = FanSpeed.Config(
            speeds={
                # Two GPUs with same mapping
                ("gpu", -1, 0): (
                    (0.0, 15.0, 5.0, None),
                    (70.0, 80.0, 5.0, None),
                    (80.0, 100.0, 5.0, None),
                ),
            },
            hysteresis_celsius=5.0,
            hysteresis_seconds=0.0,  # Immediate drop for easier testing
        ).setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)

        # Both GPUs hot - both at 100%
        hardware.temps = {"gpu": (85, 85)}
        daemon.control_loop()
        assert hardware.zone_speeds[0] == 100
        # Both should have active_threshold=80
        assert daemon.active_thresholds[("gpu", 0, 0)] == 80.0
        assert daemon.active_thresholds[("gpu", 1, 0)] == 80.0

        # gpu0 cools to 20C, gpu1 stays hot at 85C
        # gpu0 should drop to threshold=0, gpu1 stays at threshold=80
        hardware.temps = {"gpu": (20, 85)}
        daemon.control_loop()
        assert hardware.zone_speeds[0] == 100  # gpu1 wins
        # CRITICAL: gpu0's threshold must be updated even though it lost
        assert daemon.active_thresholds[("gpu", 0, 0)] == 0.0  # Dropped!
        assert daemon.active_thresholds[("gpu", 1, 0)] == 80.0  # Still high

        # Now gpu1 also cools - should drop immediately since gpu0 already dropped
        hardware.temps = {"gpu": (20, 20)}
        daemon.control_loop()
        # With the bug, gpu0 would restart its timer here because its
        # threshold was stale. With the fix, both are at threshold=0.
        assert hardware.zone_speeds[0] == 15  # Both at low speed
        assert daemon.active_thresholds[("gpu", 0, 0)] == 0.0
        assert daemon.active_thresholds[("gpu", 1, 0)] == 0.0

    def test_control_loop_set_zone_speed_failure(
        self, daemon: FanDaemon, hardware: MockHardware
    ) -> None:
        # Make set_zone_speed fail
        def fail_set_zone_speed(zone: int, percent: int) -> bool:  # noqa: ARG001
            del zone, percent
            return False

        hardware.set_zone_speed = fail_set_zone_speed  # type: ignore[method-assign]
        daemon.control_loop()
        assert hardware.fail_safe_called

    def test_compute_zone_speeds_no_candidates(self, hardware: MockHardware) -> None:
        # Use empty mappings
        hardware.zones = (0,)
        fan_speed = FanSpeed.Config(speeds={}).setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)
        assert hardware.temps is not None
        hardware.temps["cpu"] = None
        hardware.temps["gpu"] = None
        temps = hardware.get_temps()
        assert temps is not None
        speeds = daemon._compute_zone_speeds(temps)
        assert speeds[0] == (100, "none", 0)  # fail-safe when no mappings

    def test_arbitrary_device_key_decoupled(self) -> None:
        """Prove hardware and mapping flags are decoupled.

        Hardware can return any arbitrary key, and a --speeds flag can
        reference that key without any hardcoded coupling in FanDaemon.
        """
        # Parse a mapping for an arbitrary device type "custom_sensor"
        key, mapping = FanSpeed.Config._parse_speeds("custom_sensor-zone0=50:30,80:100")
        assert key == ("custom_sensor", -1, 0)
        assert mapping is not None

        # Create config with this custom mapping
        fan_speed = FanSpeed.Config(speeds={key: mapping}).setup()

        # Create mock hardware that returns the arbitrary key
        @final
        class CustomHardware:
            zones = (0,)

            def initialize(self) -> bool:
                return True

            def get_temps(self) -> dict[str, tuple[int, ...] | None]:
                return {
                    "custom_sensor": (75,),  # Arbitrary key from hardware
                    "another_sensor": (40,),  # Another arbitrary key, no mapping
                }

            def get_zones(self) -> tuple[int, ...]:
                return self.zones

            def set_zone_speed(self, _zone: int, _percent: int) -> bool:
                return True

            def set_fail_safe(self) -> bool:
                return True

        daemon = FanDaemon.Config().setup(CustomHardware(), fan_speed)
        temps = daemon.hardware.get_temps()
        assert temps is not None
        speeds = daemon._compute_zone_speeds(temps)

        # custom_sensor at 75°C should trigger the 50:30 threshold -> 30%
        assert speeds[0][0] == 30
        assert speeds[0][1] == "CUSTOM_SENSOR0"
        assert speeds[0][2] == 75


class TestSupermicroH13:
    """Tests for SupermicroH13 hardware class with mocked sensors."""

    @pytest.fixture
    def hw(self) -> SupermicroH13:
        hw = SupermicroH13.Config(ipmi_write_delay_seconds=0.0).setup()
        hw._sensors = _make_mock_sensors()
        return hw

    def test_get_temps_success(self, hw: SupermicroH13) -> None:
        hw._sensors = _make_mock_sensors(
            cpu=(45.0,), gpu=(65.0, 70.0), hdd=(35.0,), nvme=(42.0,)
        )
        temps = hw.get_temps()
        assert temps is not None
        assert temps["cpu"] == (45,)
        assert temps["gpu"] == (65, 70)
        assert temps["hdd"] == (35,)
        assert temps["nvme"] == (42,)

    def test_get_temps_cpu_failure(self, hw: SupermicroH13) -> None:
        hw._sensors = _make_mock_sensors(cpu=None, gpu=(65.0,))
        temps = hw.get_temps()
        assert temps is None

    def test_get_temps_gpu_failure(self, hw: SupermicroH13) -> None:
        hw._sensors = _make_mock_sensors(cpu=(45.0,), gpu=None)
        temps = hw.get_temps()
        assert temps is None

    def test_get_temps_with_hdd(self, hw: SupermicroH13) -> None:
        hw._sensors = _make_mock_sensors(hdd=(35.0,))
        temps = hw.get_temps()
        assert temps is not None
        assert temps["hdd"] == (35,)

    def test_get_temps_with_nvme(self, hw: SupermicroH13) -> None:
        hw._sensors = _make_mock_sensors(nvme=(42.0,))
        temps = hw.get_temps()
        assert temps is not None
        assert temps["nvme"] == (42,)

    def test_set_zone_speed_success(self, hw: SupermicroH13) -> None:
        def mockrun_cmd(cmd: list[str], _timeout: float = 5.0) -> str | None:
            if "0x45" in cmd and "0x00" in cmd:
                return "01"  # Already in full mode
            return ""

        with patch.object(_module, "run_cmd", side_effect=mockrun_cmd):
            result = hw.set_zone_speed(0, 50)
        assert result is True

    def test_set_zone_speed_skips_if_unchanged(self, hw: SupermicroH13) -> None:
        """Setting same speed twice skips the IPMI call."""
        call_count = 0

        def mockrun_cmd(cmd: list[str], _timeout: float = 5.0) -> str | None:
            nonlocal call_count
            if "0x66" in cmd:
                call_count += 1
            if "0x45" in cmd and "0x00" in cmd:
                return "01"
            return ""

        with patch.object(_module, "run_cmd", side_effect=mockrun_cmd):
            # First call should set speed
            result1 = hw.set_zone_speed(0, 50)
            assert result1 is True
            assert call_count == 1
            # Second call with same speed should skip
            result2 = hw.set_zone_speed(0, 50)
            assert result2 is True
            assert call_count == 1  # No additional call

    def test_set_zone_speed_failure(self, hw: SupermicroH13) -> None:
        def mockrun_cmd(cmd: list[str], _timeout: float = 5.0) -> str | None:
            if "0x45" in cmd and "0x00" in cmd:
                return "01"  # Already in full mode
            if "0x66" in cmd:
                return None  # Fail the set speed command
            return ""

        with patch.object(_module, "run_cmd", side_effect=mockrun_cmd):
            result = hw.set_zone_speed(0, 50)
        assert result is False

    def test_set_fail_safe(self, hw: SupermicroH13) -> None:
        def mockrun_cmd(cmd: list[str], _timeout: float = 5.0) -> str | None:
            if "0x45" in cmd and "0x00" in cmd:
                return "01"  # Already in full mode
            return ""

        with patch.object(_module, "run_cmd", side_effect=mockrun_cmd):
            result = hw.set_fail_safe()
        assert result is True

    def test_initialize(self, hw: SupermicroH13) -> None:
        def mockrun_cmd(cmd: list[str], _timeout: float = 5.0) -> str | None:
            if "0x45" in cmd and "0x00" in cmd:
                return "01"  # Already in full mode
            return ""

        with patch.object(_module, "run_cmd", side_effect=mockrun_cmd):
            result = hw.initialize()
        assert result is True

    def test_get_zones(self, hw: SupermicroH13) -> None:
        assert hw.get_zones() == (0, 1)

    def test_set_full_mode_needs_set(self, hw: SupermicroH13) -> None:
        call_sequence: list[str] = []

        def mockrun_cmd(cmd: list[str], _timeout: float = 5.0) -> str | None:
            call_sequence.append(" ".join(cmd))
            if "0x45" in cmd and "0x00" in cmd:
                return "00"  # Not in full mode
            if "0x45" in cmd and "0x01" in cmd:
                return ""  # Set mode success
            return ""

        def noop_sleep(_seconds: float) -> None:
            pass

        with patch.object(_module, "run_cmd", side_effect=mockrun_cmd):
            with patch.object(_module, "time") as mock_time:
                mock_time.sleep = noop_sleep  # type: ignore[assignment]
                result = hw._set_full_mode()
        assert result is True
        assert any("0x01 0x01" in c for c in call_sequence)

    def test_set_full_mode_failure(self, hw: SupermicroH13) -> None:
        def mockrun_cmd(cmd: list[str], _timeout: float = 5.0) -> str | None:
            if "0x45" in cmd:
                return None  # All mode commands fail
            return ""

        with patch.object(_module, "run_cmd", side_effect=mockrun_cmd):
            result = hw._set_full_mode()
        assert result is False

    def test_ipmi_temps_adds_ipmitool_sensor(self) -> None:
        """When ipmi_temps=True, Ipmitool sensor is added."""
        hw = SupermicroH13.Config(ipmi_temps=True).setup()
        sensor_types = [type(s).__name__ for s in hw._sensors]
        assert "Ipmitool" in sensor_types

    def test_ipmi_temps_false_no_ipmitool_sensor(self) -> None:
        """When ipmi_temps=False (default), no Ipmitool sensor."""
        hw = SupermicroH13.Config(ipmi_temps=False).setup()
        sensor_types = [type(s).__name__ for s in hw._sensors]
        assert "Ipmitool" not in sensor_types


class TestConfigFromArgs:
    """Tests for Config.from_args methods."""

    def test_fan_speed_config_from_args(self) -> None:
        import argparse

        argparser = argparse.ArgumentParser()
        FanSpeed.Config.add_args(argparser)
        args = argparser.parse_args(
            ["--speeds", "gpu=50:20,80:100", "--hysteresis_celsius", "3"]
        )
        config = FanSpeed.Config.from_args(argparser, args)
        assert config.hysteresis_celsius == 3.0
        assert ("gpu", -1, -1) in config.speeds

    def test_fan_speed_config_from_args_no_speeds(self) -> None:
        import argparse

        argparser = argparse.ArgumentParser()
        FanSpeed.Config.add_args(argparser)
        args = argparser.parse_args([])
        config = FanSpeed.Config.from_args(argparser, args)
        assert config.hysteresis_celsius == 5.0  # default

    def test_supermicro_h13_config_from_args(self) -> None:
        import argparse

        argparser = argparse.ArgumentParser()
        SupermicroH13.Config.add_args(argparser)
        args = argparser.parse_args([])
        config = SupermicroH13.Config.from_args(argparser, args)
        assert config.zones == (0, 1)

    def test_fan_daemon_config_from_args(self) -> None:
        import argparse

        argparser = argparse.ArgumentParser()
        FanDaemon.Config.add_args(argparser)
        args = argparser.parse_args(
            ["--interval_seconds", "10", "--heartbeat_seconds", "60"]
        )
        config = FanDaemon.Config.from_args(argparser, args)
        assert config.interval_seconds == 10.0
        assert config.heartbeat_seconds == 60.0

    def test_fan_daemon_config_from_args_defaults(self) -> None:
        import argparse

        argparser = argparse.ArgumentParser()
        FanDaemon.Config.add_args(argparser)
        args = argparser.parse_args([])
        config = FanDaemon.Config.from_args(argparser, args)
        assert config.interval_seconds == 5.0
        assert config.heartbeat_seconds == 30.0


class TestUtilityFunctions:
    """Tests for module-level utility functions."""

    def testrun_cmd_success(self) -> None:
        result = run_cmd(["echo", "hello"])
        assert result == "hello\n"

    def testrun_cmd_failure(self) -> None:
        result = run_cmd(["false"])
        assert result is None

    def testrun_cmd_timeout(self) -> None:
        result = run_cmd(["sleep", "10"], timeout=0.1)
        assert result is None

    def testrun_cmd_not_found(self) -> None:
        result = run_cmd(["nonexistent_command_12345"])
        assert result is None


class TestFanDaemonLifecycle:
    """Tests for FanDaemon run/shutdown lifecycle."""

    def test_format_status(self) -> None:
        hardware = MockHardware()
        fan_speed = FanSpeed.Config().setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)

        # (speed, trigger, temp)
        zone_speeds = {0: (50, "GPU0", 70), 1: (30, "CPU0", 45)}
        temps = {"cpu": (45,), "gpu": (70,), "ram": None}

        status = daemon._format_status(zone_speeds, temps)
        # Check zone summary line
        assert "z0=50%" in status
        assert "z1=30%" in status
        # Check device lines with temps
        assert "cpu0" in status
        assert "45C" in status
        assert "gpu0" in status
        assert "70C" in status
        # Check winner markers
        assert "<-- z0" in status
        assert "<-- z1" in status

    def test_format_status_with_informational_sensors(self) -> None:
        """Sensors without speed curves (gpu_ipmi, vrm_cpu) appear in output."""
        hardware = MockHardware()
        # Only gpu has a curve in defaults, gpu_ipmi and vrm_cpu don't
        fan_speed = FanSpeed.Config(
            speeds={
                ("gpu", -1, -1): ((0.0, 15.0, None, None), (80.0, 100.0, None, None))
            }
        ).setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)

        zone_speeds = {0: (50, "GPU0", 70)}
        # gpu_ipmi and vrm_cpu have no speed curves - informational only
        temps = {"gpu": (70,), "gpu_ipmi": (72,), "vrm_cpu": (45,)}

        status = daemon._format_status(zone_speeds, temps)
        # Devices with curves show zone contributions
        assert "gpu0" in status
        assert "70C" in status
        # Devices without curves still appear (informational)
        assert "gpu_ipmi0" in status
        assert "72C" in status
        assert "vrm_cpu0" in status
        assert "45C" in status

    def test_heartbeat_logging(self) -> None:
        hardware = MockHardware()
        fan_speed = FanSpeed.Config().setup()
        config = FanDaemon.Config(heartbeat_seconds=0.01)
        daemon = config.setup(hardware, fan_speed)

        # First call logs and sets heartbeat
        daemon.control_loop()
        first_heartbeat = daemon.last_heartbeat

        # Second call with same speeds - no log (speeds unchanged, heartbeat not due)
        daemon.control_loop()

        # Wait for heartbeat interval
        import time

        time.sleep(0.02)

        # Third call should trigger heartbeat log
        daemon.control_loop()
        assert daemon.last_heartbeat > first_heartbeat

    def test_shutdown(self) -> None:
        hardware = MockHardware()
        fan_speed = FanSpeed.Config().setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)

        with pytest.raises(SystemExit):
            daemon.shutdown(signum=15)
        assert hardware.fail_safe_called


class TestFromArgsErrors:
    """Tests for from_args error paths."""

    def test_fan_speed_config_from_args_invalid_speeds(self) -> None:
        """Test from_args with invalid speeds spec triggers argparser.error."""
        import argparse

        argparser = argparse.ArgumentParser()
        FanSpeed.Config.add_args(argparser)
        # Use invalid spec that will cause _parse_speeds to raise ValueError
        args = argparser.parse_args(["--speeds", "invalid"])

        with pytest.raises(SystemExit):  # argparser.error() calls sys.exit
            FanSpeed.Config.from_args(argparser, args)

    def test_fan_daemon_config_from_args_interval_zero(self) -> None:
        """Test from_args with interval_seconds <= 0 triggers argparser.error."""
        import argparse

        argparser = argparse.ArgumentParser()
        FanDaemon.Config.add_args(argparser)
        args = argparser.parse_args(["--interval_seconds", "0"])

        with pytest.raises(SystemExit):  # argparser.error() calls sys.exit
            FanDaemon.Config.from_args(argparser, args)

    def test_fan_daemon_config_from_args_interval_negative(self) -> None:
        """Test from_args with negative interval_seconds triggers argparser.error."""
        import argparse

        argparser = argparse.ArgumentParser()
        FanDaemon.Config.add_args(argparser)
        args = argparser.parse_args(["--interval_seconds", "-5"])

        with pytest.raises(SystemExit):  # argparser.error() calls sys.exit
            FanDaemon.Config.from_args(argparser, args)


class TestFanDaemonRun:
    """Tests for FanDaemon.run() method."""

    def test_run_calls_initialize(self) -> None:
        """Test that run() calls hardware.initialize()."""
        hardware = MockHardware()
        initialized = []

        def mock_initialize() -> bool:
            initialized.append(True)
            return True

        hardware.initialize = mock_initialize  # type: ignore[method-assign]

        fan_speed = FanSpeed.Config().setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)

        # Stop after one loop iteration
        def stop_after_one_iteration(_seconds: float) -> None:
            daemon.running = False

        with patch.object(_module, "time") as mock_time:
            mock_time.sleep.side_effect = stop_after_one_iteration
            mock_time.time.return_value = 0.0
            daemon.run()

        assert len(initialized) == 1

    def test_run_control_loop_exception_sets_fail_safe(self) -> None:
        """Test that exceptions in control_loop trigger fail-safe."""
        hardware = MockHardware()
        fan_speed = FanSpeed.Config().setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)

        call_count = 0

        def mock_control_loop() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Test error")
            daemon.running = False

        daemon.control_loop = mock_control_loop  # type: ignore[method-assign]

        with patch.object(_module, "time") as mock_time:
            mock_time.sleep.return_value = None
            mock_time.time.return_value = 0.0
            daemon.run()

        assert hardware.fail_safe_called

    def test_run_sets_fail_safe_on_exit(self) -> None:
        """Test that run() sets fail-safe when exiting normally."""
        hardware = MockHardware()
        fan_speed = FanSpeed.Config().setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)

        # Stop immediately
        def stop_immediately(_seconds: float) -> None:
            daemon.running = False

        with patch.object(_module, "time") as mock_time:
            mock_time.sleep.side_effect = stop_immediately
            mock_time.time.return_value = 0.0
            daemon.run()

        assert hardware.fail_safe_called

    def test_run_exits_if_initialize_fails(self) -> None:
        """Test that run() exits with code 1 if hardware.initialize() fails."""
        hardware = MockHardware()
        hardware.initialize = lambda: False  # type: ignore[method-assign]

        fan_speed = FanSpeed.Config().setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)

        with pytest.raises(SystemExit) as exc_info:
            daemon.run()
        assert exc_info.value.code == 1

    def test_run_exits_if_get_temps_fails_at_startup(self) -> None:
        """Test that run() exits with code 1 if initial get_temps() returns None."""
        hardware = MockHardware()
        hardware.get_temps = lambda: None  # type: ignore[method-assign]

        fan_speed = FanSpeed.Config().setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)

        with pytest.raises(SystemExit) as exc_info:
            daemon.run()
        assert exc_info.value.code == 1

    def test_run_exits_if_mapping_references_missing_sensor(self) -> None:
        """Test that run() exits if speed mapping references non-existent sensor."""
        hardware = MockHardware()
        # Hardware only returns cpu temps, but we have a mapping for "nonexistent"
        hardware.get_temps = lambda: {"cpu": (45,)}  # type: ignore[method-assign]

        fan_speed = FanSpeed.Config(
            speeds={
                ("nonexistent", -1, -1): (
                    (0.0, 15.0, None, None),
                    (80.0, 100.0, None, None),
                )
            }
        ).setup()
        daemon = FanDaemon.Config().setup(hardware, fan_speed)

        with pytest.raises(SystemExit) as exc_info:
            daemon.run()
        assert exc_info.value.code == 1


class TestEdgeCases:
    """Edge case tests for better coverage."""

    def test_parse_speeds_empty_part(self) -> None:
        # Test with empty part between commas
        _, mapping = FanSpeed.Config._parse_speeds("x=40:15,,80:100")
        assert mapping == ((40.0, 15.0, None, None), (80.0, 100.0, None, None))

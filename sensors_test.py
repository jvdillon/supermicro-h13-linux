"""Unit tests for sensors.py."""
# pyright: basic
# ruff: noqa: SLF001

from __future__ import annotations

import pathlib
import tempfile
from unittest.mock import patch

import pytest

import sensors


class TestRunCmd:
    """Tests for run_cmd helper function."""

    def test_success(self) -> None:
        result = sensors.run_cmd(["echo", "hello"])
        assert result == "hello\n"

    def test_failure_nonzero_exit(self) -> None:
        result = sensors.run_cmd(["false"])
        assert result is None

    def test_timeout(self) -> None:
        result = sensors.run_cmd(["sleep", "10"], timeout=0.1)
        assert result is None

    def test_command_not_found(self) -> None:
        result = sensors.run_cmd(["nonexistent_command_12345"])
        assert result is None

    def test_captures_stdout(self) -> None:
        result = sensors.run_cmd(["echo", "-n", "test"])
        assert result == "test"

    def test_default_timeout(self) -> None:
        result = sensors.run_cmd(["echo", "fast"])
        assert result == "fast\n"


class TestValidTemp:
    """Tests for _valid_temp helper function."""

    def test_valid_in_range(self) -> None:
        assert sensors._valid_temp(50.0) == 50.0

    def test_valid_at_lower_boundary(self) -> None:
        assert sensors._valid_temp(0.0) == 0.0

    def test_valid_at_upper_boundary(self) -> None:
        assert sensors._valid_temp(120.0) == 120.0

    def test_invalid_below_range(self) -> None:
        assert sensors._valid_temp(-0.1) is None

    def test_invalid_above_range(self) -> None:
        assert sensors._valid_temp(120.1) is None

    def test_invalid_negative(self) -> None:
        assert sensors._valid_temp(-50.0) is None

    def test_invalid_extreme_high(self) -> None:
        assert sensors._valid_temp(1000.0) is None


class TestK10Temp:
    """Tests for K10Temp CPU sensor."""

    def test_init_finds_k10temp_hwmon(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hwmon_path = pathlib.Path(tmpdir) / "hwmon0"
            hwmon_path.mkdir()
            (hwmon_path / "name").write_text("k10temp\n")
            (hwmon_path / "temp1_input").write_text("45000\n")

            with patch.object(pathlib.Path, "iterdir", return_value=[hwmon_path]):
                sensor = sensors.K10Temp()
                assert sensor._hwmon_path == hwmon_path

    def test_init_no_k10temp_hwmon(self) -> None:
        with patch.object(pathlib.Path, "iterdir", return_value=[]):
            sensor = sensors.K10Temp()
            assert sensor._hwmon_path is None

    def test_get_no_hwmon_returns_none(self) -> None:
        sensor = sensors.K10Temp()
        sensor._hwmon_path = None
        result = sensor.get()
        assert result == {"cpu": None}

    def test_get_reads_temps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hwmon_path = pathlib.Path(tmpdir)
            (hwmon_path / "temp1_input").write_text("45000\n")
            (hwmon_path / "temp2_input").write_text("50000\n")

            sensor = sensors.K10Temp()
            sensor._hwmon_path = hwmon_path
            result = sensor.get()

            assert result["cpu"] is not None
            assert 45.0 in result["cpu"]
            assert 50.0 in result["cpu"]

    def test_get_handles_read_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hwmon_path = pathlib.Path(tmpdir)

            sensor = sensors.K10Temp()
            sensor._hwmon_path = hwmon_path
            result = sensor.get()

            assert result == {"cpu": None}

    def test_get_skips_invalid_temps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hwmon_path = pathlib.Path(tmpdir)
            (hwmon_path / "temp1_input").write_text("45000\n")
            (hwmon_path / "temp2_input").write_text("150000\n")

            sensor = sensors.K10Temp()
            sensor._hwmon_path = hwmon_path
            result = sensor.get()

            assert result["cpu"] == (45.0,)

    def test_get_handles_malformed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hwmon_path = pathlib.Path(tmpdir)
            (hwmon_path / "temp1_input").write_text("not a number\n")

            sensor = sensors.K10Temp()
            sensor._hwmon_path = hwmon_path
            result = sensor.get()

            assert result == {"cpu": None}

    def test_get_converts_millidegrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hwmon_path = pathlib.Path(tmpdir)
            (hwmon_path / "temp1_input").write_text("65500\n")

            sensor = sensors.K10Temp()
            sensor._hwmon_path = hwmon_path
            result = sensor.get()

            assert result["cpu"] == (65.5,)


class TestNvidiasmi:
    """Tests for Nvidiasmi GPU sensor."""

    @pytest.fixture
    def sensor(self) -> sensors.Nvidiasmi:
        return sensors.Nvidiasmi()

    def test_get_single_gpu(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value="65\n"):
            result = sensor.get()
        assert result == {"gpu": (65.0,)}

    def test_get_multiple_gpus(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value="65\n70\n75\n80\n"):
            result = sensor.get()
        assert result == {"gpu": (65.0, 70.0, 75.0, 80.0)}

    def test_get_command_failure(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value=None):
            result = sensor.get()
        assert result == {"gpu": None}

    def test_get_empty_output(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value=""):
            result = sensor.get()
        assert result == {"gpu": None}

    def test_get_whitespace_output(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value="   \n  \n"):
            result = sensor.get()
        assert result == {"gpu": None}

    def test_get_invalid_output(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value="not a number\n"):
            result = sensor.get()
        assert result == {"gpu": None}

    def test_get_mixed_valid_invalid(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value="65\ninvalid\n"):
            result = sensor.get()
        assert result == {"gpu": None}

    def test_get_out_of_range_high(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value="150\n"):
            result = sensor.get()
        assert result == {"gpu": None}

    def test_get_out_of_range_negative(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value="-10\n"):
            result = sensor.get()
        assert result == {"gpu": None}

    def test_get_boundary_temps(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value="0\n120\n"):
            result = sensor.get()
        assert result == {"gpu": (0.0, 120.0)}

    def test_get_calls_nvidia_smi_correctly(self, sensor: sensors.Nvidiasmi) -> None:
        with patch.object(sensors, "run_cmd", return_value="65\n") as mock:
            _ = sensor.get()
            mock.assert_called_once_with(
                [
                    "nvidia-smi",
                    "--query-gpu=temperature.gpu",
                    "--format=csv,noheader,nounits",
                ]
            )


class TestSmartctl:
    """Tests for Smartctl HDD sensor."""

    @pytest.fixture
    def sensor(self) -> sensors.Smartctl:
        sensor = sensors.Smartctl()
        sensor._devices = ()
        return sensor

    def test_get_no_devices(self, sensor: sensors.Smartctl) -> None:
        result = sensor.get()
        assert result == {"hdd": None}

    def test_get_single_device(self, sensor: sensors.Smartctl) -> None:
        sensor._devices = ("/dev/sda",)
        smartctl_out = """smartctl 7.4 2023-08-01 r5530
=== START OF READ SMART DATA SECTION ===
ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
194 Temperature_Celsius     0x0022   100   100   000    Old_age   Always       -       35
"""
        with patch.object(sensors, "run_cmd", return_value=smartctl_out):
            result = sensor.get()
        assert result == {"hdd": (35.0,)}

    def test_get_airflow_temperature(self, sensor: sensors.Smartctl) -> None:
        sensor._devices = ("/dev/sda",)
        smartctl_out = """ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
190 Airflow_Temperature     0x0022   100   100   000    Old_age   Always       -       42
"""
        with patch.object(sensors, "run_cmd", return_value=smartctl_out):
            result = sensor.get()
        assert result == {"hdd": (42.0,)}

    def test_get_multiple_devices(self, sensor: sensors.Smartctl) -> None:
        sensor._devices = ("/dev/sda", "/dev/sdb")

        def mock_smartctl(cmd: list[str], timeout: float = 5.0) -> str | None:
            if "/dev/sda" in cmd:
                return "194 Temperature_Celsius     0x0022   100   100   000    Old_age   Always       -       35\n"
            if "/dev/sdb" in cmd:
                return "194 Temperature_Celsius     0x0022   100   100   000    Old_age   Always       -       40\n"
            return None

        with patch.object(sensors, "run_cmd", side_effect=mock_smartctl):
            result = sensor.get()
        assert result == {"hdd": (35.0, 40.0)}

    def test_get_command_failure(self, sensor: sensors.Smartctl) -> None:
        sensor._devices = ("/dev/sda",)
        with patch.object(sensors, "run_cmd", return_value=None):
            result = sensor.get()
        assert result == {"hdd": None}

    def test_get_partial_failure(self, sensor: sensors.Smartctl) -> None:
        sensor._devices = ("/dev/sda", "/dev/sdb")

        def mock_smartctl(cmd: list[str], timeout: float = 5.0) -> str | None:
            if "/dev/sda" in cmd:
                return "194 Temperature_Celsius     0x0022   100   100   000    Old_age   Always       -       35\n"
            return None

        with patch.object(sensors, "run_cmd", side_effect=mock_smartctl):
            result = sensor.get()
        assert result == {"hdd": (35.0,)}

    def test_get_no_temp_line(self, sensor: sensors.Smartctl) -> None:
        sensor._devices = ("/dev/sda",)
        smartctl_out = """smartctl 7.4 2023-08-01 r5530
=== START OF READ SMART DATA SECTION ===
ID# ATTRIBUTE_NAME          FLAG     VALUE
  1 Raw_Read_Error_Rate     0x002f   100   100   051
"""
        with patch.object(sensors, "run_cmd", return_value=smartctl_out):
            result = sensor.get()
        assert result == {"hdd": None}

    def test_get_short_line(self, sensor: sensors.Smartctl) -> None:
        sensor._devices = ("/dev/sda",)
        smartctl_out = """194 Temperature_Celsius short"""
        with patch.object(sensors, "run_cmd", return_value=smartctl_out):
            result = sensor.get()
        assert result == {"hdd": None}

    def test_get_non_numeric_temp(self, sensor: sensors.Smartctl) -> None:
        sensor._devices = ("/dev/sda",)
        smartctl_out = """194 Temperature_Celsius     0x0022   100   100   000    Old_age   Always       -       na
"""
        with patch.object(sensors, "run_cmd", return_value=smartctl_out):
            result = sensor.get()
        assert result == {"hdd": None}

    def test_get_out_of_range_temp(self, sensor: sensors.Smartctl) -> None:
        sensor._devices = ("/dev/sda",)
        smartctl_out = """194 Temperature_Celsius     0x0022   100   100   000    Old_age   Always       -       150
"""
        with patch.object(sensors, "run_cmd", return_value=smartctl_out):
            result = sensor.get()
        assert result == {"hdd": None}

    def test_get_calls_smartctl_correctly(self, sensor: sensors.Smartctl) -> None:
        sensor._devices = ("/dev/sda",)
        smartctl_out = """194 Temperature_Celsius     0x0022   100   100   000    Old_age   Always       -       35
"""
        with patch.object(sensors, "run_cmd", return_value=smartctl_out) as mock:
            _ = sensor.get()
            mock.assert_called_once_with(["smartctl", "-A", "/dev/sda"])


class TestNvmecli:
    """Tests for Nvmecli NVMe sensor."""

    @pytest.fixture
    def sensor(self) -> sensors.Nvmecli:
        sensor = sensors.Nvmecli()
        sensor._devices = ()
        return sensor

    def test_get_no_devices(self, sensor: sensors.Nvmecli) -> None:
        result = sensor.get()
        assert result == {"nvme": None}

    def test_get_single_device(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1",)
        nvme_out = """Smart Log for NVME device:nvme0n1 namespace-id:ffffffff
critical_warning                        : 0
temperature                             : 42 C
available_spare                         : 100%
"""
        with patch.object(sensors, "run_cmd", return_value=nvme_out):
            result = sensor.get()
        assert result == {"nvme": (42.0,)}

    def test_get_multiple_devices(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1", "/dev/nvme1n1")

        def mock_nvme(cmd: list[str], timeout: float = 5.0) -> str | None:
            if "/dev/nvme0n1" in cmd:
                return "temperature                             : 42 C\n"
            if "/dev/nvme1n1" in cmd:
                return "temperature                             : 48 C\n"
            return None

        with patch.object(sensors, "run_cmd", side_effect=mock_nvme):
            result = sensor.get()
        assert result == {"nvme": (42.0, 48.0)}

    def test_get_command_failure(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1",)
        with patch.object(sensors, "run_cmd", return_value=None):
            result = sensor.get()
        assert result == {"nvme": None}

    def test_get_partial_failure(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1", "/dev/nvme1n1")

        def mock_nvme(cmd: list[str], timeout: float = 5.0) -> str | None:
            if "/dev/nvme0n1" in cmd:
                return "temperature                             : 42 C\n"
            return None

        with patch.object(sensors, "run_cmd", side_effect=mock_nvme):
            result = sensor.get()
        assert result == {"nvme": (42.0,)}

    def test_get_no_temp_line(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1",)
        nvme_out = """Smart Log for NVME device:nvme0n1
critical_warning                        : 0
available_spare                         : 100%
"""
        with patch.object(sensors, "run_cmd", return_value=nvme_out):
            result = sensor.get()
        assert result == {"nvme": None}

    def test_get_no_colon(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1",)
        nvme_out = """temperature no colon here
"""
        with patch.object(sensors, "run_cmd", return_value=nvme_out):
            result = sensor.get()
        assert result == {"nvme": None}

    def test_get_invalid_format(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1",)
        nvme_out = """temperature                             : invalid
"""
        with patch.object(sensors, "run_cmd", return_value=nvme_out):
            result = sensor.get()
        assert result == {"nvme": None}

    def test_get_empty_value(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1",)
        nvme_out = """temperature                             :
"""
        with patch.object(sensors, "run_cmd", return_value=nvme_out):
            result = sensor.get()
        assert result == {"nvme": None}

    def test_get_out_of_range_temp(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1",)
        nvme_out = """temperature                             : 150 C
"""
        with patch.object(sensors, "run_cmd", return_value=nvme_out):
            result = sensor.get()
        assert result == {"nvme": None}

    def test_get_temp_with_comma_stripped(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1",)
        nvme_out = """temperature                             : 42,000 C
"""
        with patch.object(sensors, "run_cmd", return_value=nvme_out):
            result = sensor.get()
        # Comma stripped: "42,000" -> "42000" -> out of range
        assert result == {"nvme": None}

    def test_get_only_first_temp_per_device(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1",)
        nvme_out = """temperature                             : 42 C
Temperature Sensor 1                    : 45 C
Temperature Sensor 2                    : 50 C
"""
        with patch.object(sensors, "run_cmd", return_value=nvme_out):
            result = sensor.get()
        assert result == {"nvme": (42.0,)}

    def test_get_calls_nvme_correctly(self, sensor: sensors.Nvmecli) -> None:
        sensor._devices = ("/dev/nvme0n1",)
        nvme_out = """temperature                             : 42 C
"""
        with patch.object(sensors, "run_cmd", return_value=nvme_out) as mock:
            _ = sensor.get()
            mock.assert_called_once_with(["nvme", "smart-log", "/dev/nvme0n1"])


class TestIpmitool:
    """Tests for Ipmitool IPMI sensor."""

    @pytest.fixture
    def sensor_map(self) -> dict[str, str]:
        return {
            "CPU Temp": "cpu",
            "DIMMA~F Temp": "ram",
            "DIMMG~L Temp": "ram",
            "GPU1 Temp": "gpu",
            "GPU2 Temp": "gpu",
        }

    @pytest.fixture
    def sensor(self, sensor_map: dict[str, str]) -> sensors.Ipmitool:
        return sensors.Ipmitool(sensor_map)

    def test_get_all_sensors(self, sensor: sensors.Ipmitool) -> None:
        ipmi_out = """CPU Temp         | 45.000     | degrees C  | ok
DIMMA~F Temp     | 26.000     | degrees C  | ok
DIMMG~L Temp     | 28.000     | degrees C  | ok
GPU1 Temp        | 65.000     | degrees C  | ok
GPU2 Temp        | 70.000     | degrees C  | ok
"""
        with patch.object(sensors, "run_cmd", return_value=ipmi_out):
            result = sensor.get()
        assert result["cpu"] == (45.0,)
        assert result["ram"] == (26.0, 28.0)
        assert result["gpu"] == (65.0, 70.0)

    def test_get_command_failure(self, sensor: sensors.Ipmitool) -> None:
        with patch.object(sensors, "run_cmd", return_value=None):
            result = sensor.get()
        assert result == {"cpu": None, "ram": None, "gpu": None}

    def test_get_partial_sensors(self, sensor: sensors.Ipmitool) -> None:
        ipmi_out = """CPU Temp         | 45.000     | degrees C  | ok
"""
        with patch.object(sensors, "run_cmd", return_value=ipmi_out):
            result = sensor.get()
        assert result["cpu"] == (45.0,)
        assert result["ram"] is None
        assert result["gpu"] is None

    def test_get_non_numeric_value(self, sensor: sensors.Ipmitool) -> None:
        ipmi_out = """CPU Temp         | na         | degrees C  | na
DIMMA~F Temp     | 26.000     | degrees C  | ok
"""
        with patch.object(sensors, "run_cmd", return_value=ipmi_out):
            result = sensor.get()
        assert result["cpu"] is None
        assert result["ram"] == (26.0,)

    def test_get_short_line(self, sensor: sensors.Ipmitool) -> None:
        ipmi_out = """CPU Temp         | 45.000     | degrees C  | ok
Short
DIMMA~F Temp     | 26.000     | degrees C  | ok
"""
        with patch.object(sensors, "run_cmd", return_value=ipmi_out):
            result = sensor.get()
        assert result["cpu"] == (45.0,)
        assert result["ram"] == (26.0,)

    def test_get_unknown_sensor_ignored(self, sensor: sensors.Ipmitool) -> None:
        ipmi_out = """CPU Temp         | 45.000     | degrees C  | ok
Unknown Sensor   | 99.000     | degrees C  | ok
DIMMA~F Temp     | 26.000     | degrees C  | ok
"""
        with patch.object(sensors, "run_cmd", return_value=ipmi_out):
            result = sensor.get()
        assert result["cpu"] == (45.0,)
        assert result["ram"] == (26.0,)
        assert "unknown" not in result

    def test_get_out_of_range_temp(self, sensor: sensors.Ipmitool) -> None:
        ipmi_out = """CPU Temp         | 150.000    | degrees C  | ok
"""
        with patch.object(sensors, "run_cmd", return_value=ipmi_out):
            result = sensor.get()
        assert result["cpu"] is None

    def test_get_multiple_same_key(self) -> None:
        sensor = sensors.Ipmitool(
            {
                "VRM0 Temp": "vrm",
                "VRM1 Temp": "vrm",
                "VRM2 Temp": "vrm",
            }
        )
        ipmi_out = """VRM0 Temp        | 40.000     | degrees C  | ok
VRM1 Temp        | 42.000     | degrees C  | ok
VRM2 Temp        | 44.000     | degrees C  | ok
"""
        with patch.object(sensors, "run_cmd", return_value=ipmi_out):
            result = sensor.get()
        assert result["vrm"] == (40.0, 42.0, 44.0)

    def test_get_empty_sensor_map(self) -> None:
        sensor = sensors.Ipmitool({})
        ipmi_out = """CPU Temp         | 45.000     | degrees C  | ok
"""
        with patch.object(sensors, "run_cmd", return_value=ipmi_out):
            result = sensor.get()
        assert result == {}

    def test_get_whitespace_handling(self, sensor: sensors.Ipmitool) -> None:
        ipmi_out = """  CPU Temp       | 45.000     | degrees C  | ok
"""
        with patch.object(sensors, "run_cmd", return_value=ipmi_out):
            result = sensor.get()
        assert result["cpu"] == (45.0,)

    def test_get_calls_ipmitool_correctly(self, sensor: sensors.Ipmitool) -> None:
        ipmi_out = """CPU Temp         | 45.000     | degrees C  | ok
"""
        with patch.object(sensors, "run_cmd", return_value=ipmi_out) as mock:
            _ = sensor.get()
            mock.assert_called_once_with(["ipmitool", "sensor"])


class TestSensorProtocol:
    """Tests verifying sensors implement the Sensor protocol."""

    def test_k10temp_has_get(self) -> None:
        with patch.object(pathlib.Path, "iterdir", return_value=[]):
            sensor = sensors.K10Temp()
        assert hasattr(sensor, "get")
        assert callable(sensor.get)

    def test_nvidiasmi_has_get(self) -> None:
        sensor = sensors.Nvidiasmi()
        assert hasattr(sensor, "get")
        assert callable(sensor.get)

    def test_smartctl_has_get(self) -> None:
        with patch.object(pathlib.Path, "iterdir", return_value=[]):
            sensor = sensors.Smartctl()
        assert hasattr(sensor, "get")
        assert callable(sensor.get)

    def test_nvmecli_has_get(self) -> None:
        with patch.object(pathlib.Path, "glob", return_value=[]):
            sensor = sensors.Nvmecli()
        assert hasattr(sensor, "get")
        assert callable(sensor.get)

    def test_ipmitool_has_get(self) -> None:
        sensor = sensors.Ipmitool({})
        assert hasattr(sensor, "get")
        assert callable(sensor.get)

    def test_all_sensors_return_dict(self) -> None:
        with patch.object(pathlib.Path, "iterdir", return_value=[]):
            k10 = sensors.K10Temp()
        assert isinstance(k10.get(), dict)

        with patch.object(sensors, "run_cmd", return_value=None):
            assert isinstance(sensors.Nvidiasmi().get(), dict)

        with patch.object(pathlib.Path, "iterdir", return_value=[]):
            smartctl = sensors.Smartctl()
        assert isinstance(smartctl.get(), dict)

        with patch.object(pathlib.Path, "glob", return_value=[]):
            nvmecli = sensors.Nvmecli()
        assert isinstance(nvmecli.get(), dict)

        assert isinstance(sensors.Ipmitool({}).get(), dict)

#!/usr/bin/env python3
# pyright: basic
"""
Visualize fan-daemon temperature logs from journalctl.

Parses logs, stores data in npz, generates plots.

Usage:
    ./visualize-temps.py                    # scrape since fan-daemon last started
    ./visualize-temps.py --all              # scrape all history
    ./visualize-temps.py --since "2 hours"  # scrape last 2 hours (relative)
    ./visualize-temps.py --since "2026-01-04 10:30:00"  # scrape since time (absolute)
    ./visualize-temps.py --npz data.npz     # load existing npz, skip collection
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

RESULTS_DIR = Path("results")
DEFAULT_NPZ = RESULTS_DIR / "temps.npz"
DEFAULT_PNG = RESULTS_DIR / "temps.png"


def parse_flexible_datetime(s: str) -> float:
    """Parse datetime or relative time.

    Supports:
    - Absolute: "YYYY-MM-DD HH:MM:SS" (flexible delimiters, 2 or 4 digit year)
    - Relative: "2 hours", "30 minutes", "1 day", "2h", "30m", "1d"
    """
    s = s.strip().lower()

    # Try relative time first
    relative_match = re.match(
        r"^(\d+)\s*(h|hour|hours|m|min|mins|minute|minutes|d|day|days|s|sec|secs|second|seconds)$",
        s,
    )
    if relative_match:
        value = int(relative_match.group(1))
        unit = relative_match.group(2)
        if unit in ("h", "hour", "hours"):
            delta = value * 3600
        elif unit in ("m", "min", "mins", "minute", "minutes"):
            delta = value * 60
        elif unit in ("d", "day", "days"):
            delta = value * 86400
        else:  # seconds
            delta = value
        return time.time() - delta

    # Fall back to absolute datetime
    parts = re.split(r"\D+", s)
    parts = [p for p in parts if p]  # Remove empty strings
    if len(parts) != 6:
        raise ValueError(
            f"Expected 6 components (YYYY MM DD HH MM SS) or relative time (e.g., '2 hours'), got: {s}"
        )
    year, month, day, hour, minute, second = (int(p) for p in parts)
    if year < 100:
        year += 2000
    dt = datetime(year, month, day, hour, minute, second)
    return dt.timestamp()


def get_service_start_time() -> float | None:
    """Get the timestamp when fan-daemon service last started."""
    result = subprocess.run(
        ["systemctl", "show", "fan-daemon", "--property=ActiveEnterTimestamp"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    # Output: "ActiveEnterTimestamp=Sat 2026-01-04 08:30:00 PST"
    line = result.stdout.strip()
    if "=" not in line:
        return None
    timestamp_str = line.split("=", 1)[1].strip()
    if not timestamp_str:
        return None
    try:
        # Parse systemd timestamp format
        dt = datetime.strptime(timestamp_str, "%a %Y-%m-%d %H:%M:%S %Z")
        return dt.timestamp()
    except ValueError:
        return None


def parse_journalctl(since: float | None = None) -> dict[str, list]:
    """Parse journalctl output for fan-daemon logs.

    Args:
        since: Unix timestamp to start from, or None for all.

    Returns:
        Dict with 'timestamps' and device/zone keys mapping to lists.
    """
    cmd = ["journalctl", "-u", "fan-daemon", "--no-pager", "--output=short-iso"]
    if since is not None and since > 0:
        cmd.extend(["--since", f"@{int(since)}"])

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"journalctl failed: {result.stderr}", file=sys.stderr)
        return {}

    return parse_logs(result.stdout)


def parse_logs(log_text: str) -> dict[str, list]:
    """Parse log text into data structure.

    Returns:
        Dict with 'timestamps' and device/zone keys mapping to lists.
    """
    data: dict[str, list] = {"timestamps": []}
    known_keys: set[str] = set()  # Track all keys seen so far

    current_timestamp: float | None = None
    current_zones: dict[str, int] = {}
    current_temps: dict[str, int] = {}

    # Pattern: "2024-01-04T10:08:06-08:00 host fan-daemon[123]: MSG"
    line_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})\s+\S+\s+fan-daemon\[\d+\]:\s*(.*)"
    )
    # Header: "INFO: z0=15% z1=40%" or "z0=15% z1=40%"
    zone_pattern = re.compile(r"z(\d+)=(\d+)%")
    # Device: "cpu0    33C  z0:15%" (whitespace stripped by line_pattern)
    device_pattern = re.compile(r"^(\w+?)(\d+)\s+(\d+)C")

    def flush_sample():
        """Save current sample if we have data."""
        nonlocal current_timestamp, current_zones, current_temps
        if current_timestamp is not None and (current_zones or current_temps):
            data["timestamps"].append(current_timestamp)

            # Collect all values for this sample
            sample_values: dict[str, float] = {}
            for zone, speed in current_zones.items():
                key = f"z{zone}"
                sample_values[key] = speed
                known_keys.add(key)
            for device, temp in current_temps.items():
                sample_values[device] = temp
                known_keys.add(device)

            # Append value or NaN for all known keys
            for key in known_keys:
                if key not in data:
                    # New key - backfill with NaN for previous samples
                    data[key] = [float("nan")] * (len(data["timestamps"]) - 1)
                data[key].append(sample_values.get(key, float("nan")))

        current_timestamp = None
        current_zones = {}
        current_temps = {}

    for line in log_text.splitlines():
        match = line_pattern.match(line)
        if not match:
            continue

        timestamp_str, msg = match.groups()

        # Parse ISO timestamp to epoch
        try:
            dt = datetime.fromisoformat(timestamp_str)
            timestamp = dt.timestamp()
        except ValueError:
            continue

        # Check if this is a header line (contains z0=N%)
        zone_matches = zone_pattern.findall(msg)
        if zone_matches:
            # New sample starting - flush previous
            if current_timestamp is not None and timestamp != current_timestamp:
                flush_sample()
            current_timestamp = timestamp
            current_zones = {z: int(pct) for z, pct in zone_matches}
            continue

        # Check if this is a device line
        device_match = device_pattern.match(msg)
        if device_match:
            device_type, idx, temp = device_match.groups()
            device_key = f"{device_type}{idx}"
            current_temps[device_key] = int(temp)

    # Flush last sample
    flush_sample()

    return data


def align_data(data: dict[str, list]) -> dict[str, np.ndarray]:
    """Convert lists to numpy arrays.

    Returns:
        Dict with numpy arrays (alignment already handled in parse_logs).
    """
    if not data or "timestamps" not in data or not data["timestamps"]:
        return {}

    return {key: np.array(values, dtype=np.float64) for key, values in data.items()}


def load_npz(path: Path) -> dict[str, np.ndarray]:
    """Load data from npz file."""
    if not path.exists():
        return {}
    npz = np.load(path)
    return {k: npz[k] for k in npz.files}


def save_npz(data: dict[str, np.ndarray], path: Path) -> None:
    """Save data to npz file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, **data)
    print(f"Saved {path} ({len(data.get('timestamps', []))} samples)")


def merge_data(
    old: dict[str, np.ndarray],
    new: dict[str, np.ndarray],
) -> dict[str, np.ndarray]:
    """Merge old and new data, avoiding duplicates by timestamp."""
    if not old:
        return new
    if not new:
        return old

    old_ts = set(old.get("timestamps", []))

    # Find indices in new that aren't in old
    new_ts = new.get("timestamps", np.array([]))
    mask = np.array([t not in old_ts for t in new_ts])

    if not mask.any():
        return old  # Nothing new

    # Get all keys from both
    all_keys = set(old.keys()) | set(new.keys())

    result: dict[str, np.ndarray] = {}
    for key in all_keys:
        old_arr = old.get(key, np.array([]))
        new_arr = new.get(key, np.full(len(new_ts), np.nan))
        new_filtered = new_arr[mask]
        result[key] = np.concatenate([old_arr, new_filtered])

    return result


def plot_data(
    data: dict[str, np.ndarray], path: Path, since: float | None = None
) -> None:
    """Generate plot with temps and fan speeds.

    Args:
        data: Dict with numpy arrays.
        path: Output path for PNG.
        since: If provided, only show data from this timestamp onwards.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print("matplotlib not installed, skipping plot", file=sys.stderr)
        return

    if not data or "timestamps" not in data:
        print("No data to plot", file=sys.stderr)
        return

    timestamps = data["timestamps"]
    if len(timestamps) == 0:
        print("No samples to plot", file=sys.stderr)
        return

    # Convert to datetime for x-axis
    dates = [datetime.fromtimestamp(t) for t in timestamps]

    # Separate zones (fan speeds) from devices (temps)
    zones = {k: v for k, v in data.items() if k.startswith("z") and k[1:].isdigit()}
    devices = {k: v for k, v in data.items() if k not in zones and k != "timestamps"}

    fig, ax1 = plt.subplots(figsize=(14, 7))

    # Downsample markers to ~200 points max
    marker_every = max(1, len(dates) // 200)

    # Plot temps on left y-axis
    cmap = plt.get_cmap("tab10")
    colors_temp = [cmap(i / len(devices)) for i in range(len(devices))]
    for (name, temps), color in zip(sorted(devices.items()), colors_temp):
        ax1.plot(
            dates,  # pyright: ignore[reportArgumentType]
            temps,
            label=name,
            color=color,
            alpha=0.8,
            linewidth=0.8,
            marker=".",
            markersize=2,
            markevery=marker_every,
        )

    ax1.set_xlabel("Time")
    ax1.set_ylabel("Temperature (C)")
    ax1.set_ylim(0, 100)
    ax1.set_yticks(range(0, 101, 10))
    ax1.grid(True, alpha=0.3)

    # Plot fan speeds on right y-axis
    ax2 = ax1.twinx()
    colors_zone = ["red", "darkred", "firebrick", "indianred"]
    for i, (name, speeds) in enumerate(sorted(zones.items())):
        color = colors_zone[i % len(colors_zone)]
        ax2.plot(
            dates,  # pyright: ignore[reportArgumentType]
            speeds,
            label=name,
            color=color,
            linestyle="--",
            linewidth=1,
            marker=".",
            markersize=2,
            markevery=marker_every,
        )

    ax2.set_ylabel("Fan Speed (%)")
    ax2.set_ylim(0, 100)
    ax2.set_yticks(range(0, 101, 10))

    # Set x-axis limits and ticks
    if since is not None:
        xlim_left = datetime.fromtimestamp(since)
    else:
        xlim_left = dates[0]
    xlim_right = dates[-1]
    ax1.set_xlim(xlim_left, xlim_right)  # pyright: ignore[reportArgumentType]

    # Choose tick interval to get ~18 ticks with nice round minutes
    range_minutes = (xlim_right - xlim_left).total_seconds() / 60
    ideal_interval = range_minutes / 18
    # Round to nearest "nice" interval
    nice_intervals = [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440]
    interval = min(nice_intervals, key=lambda x: abs(x - ideal_interval))
    if interval >= 60:
        ax1.xaxis.set_major_locator(mdates.HourLocator(interval=interval // 60))
    else:
        ax1.xaxis.set_major_locator(mdates.MinuteLocator(interval=interval))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()

    # Combined legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=8)

    plt.title("Fan Daemon: Temperatures and Fan Speeds")
    plt.tight_layout()

    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"Saved {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Scrape logs since time: relative ('2 hours', '30m') or absolute ('2026-01-04 10:30:00').",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrape all history (default: since service start).",
    )
    parser.add_argument(
        "--npz",
        type=Path,
        default=None,
        help="Load from this npz file (skips collection, just plots).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_NPZ,
        help=f"Output npz path (default: {DEFAULT_NPZ}).",
    )
    parser.add_argument(
        "--png",
        type=Path,
        default=DEFAULT_PNG,
        help=f"Output png path (default: {DEFAULT_PNG}).",
    )
    args = parser.parse_args()

    # Parse --since if provided (used for both scraping and plot xlim)
    since_ts: float | None = None
    if args.since is not None:
        try:
            since_ts = parse_flexible_datetime(args.since)
        except ValueError as e:
            parser.error(str(e))

    if args.npz:
        # Load existing npz, skip collection
        data = load_npz(args.npz)
        if not data:
            print(f"Failed to load {args.npz}", file=sys.stderr)
            sys.exit(1)
    else:
        # Load existing data for merging
        old_data = load_npz(args.output)

        # Determine scrape mode
        if args.all:
            print("Scraping all history")
            new_data = parse_journalctl(since=None)
        elif since_ts is not None:
            print(f"Scraping since {datetime.fromtimestamp(since_ts)}")
            new_data = parse_journalctl(since=since_ts)
        else:
            # Default: since fan-daemon last started
            start_time = get_service_start_time()
            if start_time:
                print(
                    f"Scraping since service start ({datetime.fromtimestamp(start_time)})"
                )
                new_data = parse_journalctl(since=start_time)
            else:
                print("Could not get service start time, scraping all")
                new_data = parse_journalctl(since=None)

        new_data = align_data(new_data)

        if not new_data:
            if old_data:
                print("No new data, using existing")
                data = old_data
            else:
                print("No data found", file=sys.stderr)
                sys.exit(1)
        else:
            # Merge with old data
            data = merge_data(old_data, new_data)
            save_npz(data, args.output)

    # Generate plot (since_ts sets xlim if provided)
    plot_data(data, args.png, since=since_ts)


if __name__ == "__main__":
    main()

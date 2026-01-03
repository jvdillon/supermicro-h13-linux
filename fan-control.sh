#!/bin/bash
# Supermicro H13 fan control via IPMI
# Usage: ./fan-control.sh <zone> <percent>
#        ./fan-control.sh <mode>
#        ./fan-control.sh status

set -e

usage() {
    cat <<EOF
Usage: $0 <zone> <percent>
       $0 <mode>
       $0 status

Zones:
  0   - FAN1-4 (CPU, case, exhaust)
  1   - FANA-B (auxiliary/GPU)
  all - Both zones

Modes:
  standard - Standard mode
  full     - Full speed (enables manual control)
  optimal  - Optimal mode (auto)
  heavyio  - Heavy IO mode

Examples:
  $0 0 50       # Zone 0 to 50%
  $0 1 40       # Zone 1 to 40%
  $0 all 60     # All zones to 60%
  $0 optimal    # Return to auto control
  $0 status     # Show current speeds
EOF
    exit 1
}

if ! command -v ipmitool &> /dev/null; then
    echo "Error: ipmitool not installed (apt install ipmitool)"
    exit 1
fi

get_mode() {
    local mode=$(ipmitool raw 0x30 0x45 0x00 2>/dev/null | tr -d '[:blank:]')
    case $mode in
        00) echo "standard" ;;
        01) echo "full" ;;
        02) echo "optimal" ;;
        04) echo "heavyio" ;;
        *)  echo "unknown ($mode)" ;;
    esac
}

get_zone_speed() {
    local zone=$1
    local duty=$(ipmitool raw 0x30 0x70 0x66 0x00 $(printf 0x%02x $zone) 2>/dev/null | tr -d '[:blank:]')
    [ -z "$duty" ] && echo "?" && return
    echo $((16#$duty))
}

set_mode() {
    local mode=$1
    case $mode in
        standard) ipmitool raw 0x30 0x45 0x01 0x00 >/dev/null ;;
        full)     ipmitool raw 0x30 0x45 0x01 0x01 >/dev/null ;;
        optimal)  ipmitool raw 0x30 0x45 0x01 0x02 >/dev/null ;;
        heavyio)  ipmitool raw 0x30 0x45 0x01 0x04 >/dev/null ;;
        *)        echo "Unknown mode: $mode"; exit 1 ;;
    esac
}

set_zone_speed() {
    local zone=$1
    local percent=$2
    # Minimum 15% (fans floor at 15%)
    if [ "$percent" -lt 15 ]; then
        echo "Warning: Minimum is 15%"
        percent=15
    fi
    if [ "$percent" -gt 100 ]; then
        echo "Warning: Maximum is 100%"
        percent=100
    fi
    local duty_hex=$(printf 0x%02x $percent)
    ipmitool raw 0x30 0x70 0x66 0x01 $(printf 0x%02x $zone) $duty_hex >/dev/null
}

show_status() {
    echo "Mode: $(get_mode)"
    echo "Zone 0 (FAN:1,2,3,4): $(get_zone_speed 0)%"
    echo "Zone 1 (FAN:A,B): $(get_zone_speed 1)%"
}

set_zone() {
    local zone=$1
    local percent=$2
    if ! [[ "$percent" =~ ^[0-9]+$ ]] || [ "$percent" -gt 100 ]; then
        echo "Error: Percent must be 0-100"
        exit 1
    fi
    # Enable manual/full mode first (only if not already in full mode)
    if [ "$(get_mode)" != "full" ]; then
        # Save current speeds before switching (full mode resets to 100%)
        local z0_speed=$(get_zone_speed 0)
        local z1_speed=$(get_zone_speed 1)
        set_mode full
        sleep 0.5
        # Restore speeds for zones we're not changing
        if [ "$zone" = "0" ]; then
            set_zone_speed 1 "$z1_speed"
        elif [ "$zone" = "1" ]; then
            set_zone_speed 0 "$z0_speed"
        fi
    fi
    if [ "$zone" = "all" ]; then
        set_zone_speed 0 "$percent"
        set_zone_speed 1 "$percent"
    else
        set_zone_speed "$zone" "$percent"
    fi
}

[ $# -lt 1 ] && { show_status; exit 0; }

case $1 in
    help|-h|--help)
        usage
        ;;
    status)
        show_status
        ;;
    standard|full|optimal|heavyio)
        set_mode "$1"
        sleep 0.5
        show_status
        ;;
    0|1|all)
        [ $# -lt 2 ] && usage
        set_zone $1 $2
        sleep 0.5
        show_status
        ;;
    *)
        usage
        ;;
esac

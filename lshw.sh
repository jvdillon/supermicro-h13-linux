#!/bin/bash
# List hardware details for Supermicro H13SSL-N
#
# Usage: ./lshw.sh [--noserial]
#   --noserial  Hide serial numbers for privacy when sharing output
#
# Works on any Linux system with standard tools (lscpu, dmidecode, nvidia-smi, ipmitool)

# Parse arguments
HIDE_SERIAL=false
for arg in "$@"; do
    case $arg in
        --noserial) HIDE_SERIAL=true ;;
        --help) echo "Usage: $0 [--noserial]"; echo "  --noserial  Hide serial numbers for privacy"; exit 0 ;;
    esac
done

# Re-run with sudo if not root (for full hardware details)
if [ "$EUID" -ne 0 ]; then
    if command -v sudo &>/dev/null && sudo -n true 2>/dev/null; then
        # sudo available without password, re-exec
        exec sudo "$0" "$@"
    fi
    # Otherwise continue without sudo (some info will be limited)
fi

mask_serial() {
    if $HIDE_SERIAL; then
        echo "(hidden)"
    else
        echo "$1"
    fi
}

# Cache IPMI data (single call for GPUs and fans)
IPMI_SDR_DATA=$(ipmitool sdr list 2>/dev/null)
IPMI_MC_INFO=$(ipmitool mc info 2>/dev/null)

echo "Motherboard:"
echo "  Vendor:   $(cat /sys/class/dmi/id/board_vendor 2>/dev/null)"
echo "  Model:    $(cat /sys/class/dmi/id/board_name 2>/dev/null)"
echo "  Revision: $(cat /sys/class/dmi/id/board_version 2>/dev/null)"
board_serial=$(cat /sys/class/dmi/id/board_serial 2>/dev/null || echo '(run with sudo)')
echo "  Serial:   $(mask_serial "$board_serial")"
echo "  BIOS:     $(cat /sys/class/dmi/id/bios_version 2>/dev/null)"
echo "  BMC:      $(echo "$IPMI_MC_INFO" | grep 'Firmware Revision' | awk '{print $4}')"
echo ""
echo "CPU:"
# Cache lscpu output
LSCPU_DATA=$(lscpu)
echo "  Model:   $(echo "$LSCPU_DATA" | grep -m1 'Model name' | cut -d: -f2- | xargs)"
cpu_family=$(echo "$LSCPU_DATA" | grep -m1 'CPU family' | cut -d: -f2 | xargs)
cpu_model=$(echo "$LSCPU_DATA" | grep -m1 -E '^Model:' | cut -d: -f2 | xargs)
cpu_stepping=$(echo "$LSCPU_DATA" | grep -m1 'Stepping' | cut -d: -f2 | xargs)
echo "  Family:  $cpu_family, Model: $cpu_model, Stepping: $cpu_stepping"
cpu_serial=$(dmidecode -t processor 2>/dev/null | grep -m1 -i 'serial number' | cut -d: -f2 | xargs)
echo "  Serial:  $(mask_serial "${cpu_serial:-(run with sudo)}")"
echo "  Cores:   $(echo "$LSCPU_DATA" | grep -m1 'Core(s) per socket' | cut -d: -f2 | xargs) cores x $(echo "$LSCPU_DATA" | grep -m1 'Thread(s) per core' | cut -d: -f2 | xargs) threads"
echo "  Sockets: $(echo "$LSCPU_DATA" | grep -m1 'Socket(s)' | cut -d: -f2 | xargs)"
echo "  Max MHz: $(echo "$LSCPU_DATA" | grep -m1 'CPU max MHz' | cut -d: -f2 | xargs)"
echo ""
echo "Memory:"
echo "  Total: $(lsmem 2>/dev/null | grep 'Total online' | awk '{print $3, $4}')"
# Try to get DIMM details (requires root)
dimm_info=$(dmidecode -t memory 2>/dev/null | grep -E 'Size:.*[0-9]+ [GM]B' | head -1)
if [ -n "$dimm_info" ]; then
    dmidecode -t memory 2>/dev/null | awk -v hide="$HIDE_SERIAL" '
        /^Memory Device$/ { in_device=1; size=""; speed=""; mfr=""; part=""; serial=""; locator="" }
        in_device && /^\tSize:/ { size=$2" "$3 }
        in_device && /^\tSpeed:/ && !/Configured/ { speed=$2" "$3 }
        in_device && /^\tManufacturer:/ { $1=""; mfr=$0; gsub(/^[ \t]+/, "", mfr) }
        in_device && /^\tPart Number:/ { $1=""; $2=""; part=$0; gsub(/^[ \t]+/, "", part) }
        in_device && /^\tSerial Number:/ { $1=""; $2=""; serial=$0; gsub(/^[ \t]+/, "", serial) }
        in_device && /^\tLocator:/ && !/Bank/ { locator=$2 }
        in_device && /^$/ {
            if (size != "" && size !~ /No Module/ && size !~ /Unknown/) {
                printf "  %s:\n", locator
                printf "    Size:         %s\n", size
                printf "    Speed:        %s\n", speed
                printf "    Manufacturer: %s\n", mfr
                printf "    Part Number:  %s\n", part
                if (hide == "true") serial = "(hidden)"
                printf "    Serial:       %s\n", serial
            }
            in_device=0
        }
    '
else
    echo "  # Run with sudo for DIMM details (manufacturer, part number, speed)"
fi
echo ""
echo "GPUs:"
# Fetch GPU data with single nvidia-smi call; use cached IPMI data
gpu_data=$(nvidia-smi --query-gpu=pci.bus_id,temperature.gpu,name,serial,vbios_version,driver_version --format=csv,noheader 2>/dev/null)
ipmi_gpu_data=$(echo "$IPMI_SDR_DATA" | grep -i "gpu.*temp")

# Correlate PCIe bus to IPMI slot by matching temperatures
echo "$gpu_data" | while IFS=, read -r bus temp name serial vbios driver; do
    bus=$(echo "$bus" | xargs)
    temp=$(echo "$temp" | xargs)
    # Find IPMI slot with closest temperature (within 5°C)
    slot=""
    while read -r sensor_line; do
        sensor_name=$(echo "$sensor_line" | awk '{print $1}')
        ipmi_temp=$(echo "$sensor_line" | awk -F'|' '{print $2}' | grep -o '[0-9]*')
        if [ -n "$ipmi_temp" ]; then
            diff=$((temp - ipmi_temp))
            [ $diff -lt 0 ] && diff=$((-diff))
            if [ $diff -le 5 ]; then
                slot="$sensor_name"
                break
            fi
        fi
    done <<< "$ipmi_gpu_data"
    if [ -n "$slot" ]; then
        echo "  $slot ($bus):"
    else
        echo "  $bus:"
    fi
    echo "    Model:  $(echo "$name" | xargs)"
    echo "    Serial: $(mask_serial "$(echo "$serial" | xargs)")"
    echo "    VBIOS:  $(echo "$vbios" | xargs)"
    echo "    Driver: $(echo "$driver" | xargs)"
done
echo ""
echo "NVMe:"
for nvme_path in /sys/class/nvme/nvme*; do
    [ -d "$nvme_path" ] || continue
    nvme_name=$(basename "$nvme_path")
    echo "  $nvme_name:"
    echo "    Model:    $(cat "$nvme_path/model" 2>/dev/null | xargs)"
    echo "    Firmware: $(cat "$nvme_path/firmware_rev" 2>/dev/null | xargs)"
    echo "    Serial:   $(mask_serial "$(cat "$nvme_path/serial" 2>/dev/null | xargs)")"
done
echo ""
echo "SATA:"
lsblk -d -o NAME,SIZE,MODEL,SERIAL,TRAN 2>/dev/null | grep sata | while read -r name size model serial tran; do
    firmware=$(cat /sys/block/$name/device/rev 2>/dev/null | xargs)
    echo "  $name:"
    echo "    Model:    $model"
    echo "    Size:     $size"
    echo "    Firmware: $firmware"
    echo "    Serial:   $(mask_serial "$serial")"
done
echo ""
echo "Fans:"
echo "$IPMI_SDR_DATA" | grep -i fan | while read line; do
    echo "  $line"
done

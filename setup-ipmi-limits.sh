#!/bin/bash
# Example script for setting up IPMI sensor thresholds for Supermicro H13SSL-N
# Run this on a fresh system to configure optimal thresholds
#
# =============================================================================
# Presumed Hardware Configuration
# =============================================================================
#
# Motherboard:
#   Supermicro H13SSL-N
#   https://www.supermicro.com/en/products/motherboard/h13ssl-n
#   https://www.supermicro.com/manuals/motherboard/H13/MNL-2545.pdf
#
# CPU:
#   AMD EPYC 9555 (Turin/Zen 5), 64C/128T, 360W TDP [Engineering Sample]
#   https://www.amd.com/en/products/processors/server/epyc/9005-series/amd-epyc-9555.html
#   https://www.amd.com/content/dam/amd/en/documents/epyc-business-docs/datasheets/amd-epyc-9005-series-processor-datasheet.pdf
#
# Memory:
#   A-Tech 128GB Kit (4x32GB) DDR5 5600MHz PC5-44800 ECC RDIMM 1Rx4
#   Single Rank 1.1V ECC Registered DIMM 288-Pin (uses SK Hynix HMCG84AGBRA187N)
#   https://www.amazon.com/Tech-5600MHz-PC5-44800-Registered-Enterprise/dp/B0DMGHRZDW
#   https://netlist.com/wp-content/uploads/2023/07/SK-Hynix_DRAM_Server-DDR5_Combined_071823.pdf
#
# GPU:
#   2x NVIDIA GeForce RTX 5090 (slots 1 and 5)
#   https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/
#   https://www.nvidia.com/content/geforce-gtx/GeForce_RTX_5090_User_Guide_Rev1.pdf
#
# NVMe:
#   WD_Black SN8100 4TB NVMe SSD (PCIe 5.0x4, M.2 2280)
#   https://www.westerndigital.com/products/internal-drives/wd-black-sn8100-nvme-ssd
#   https://documents.sandisk.com/content/dam/asset-library/en_us/assets/public/sandisk/product/internal-drives/wd-black-ssd/data-sheet-wd-black-sn8100-nvme-ssd.pdf
#
# SATA:
#   Seagate Exos X18 12TB (ST12000NT001)
#   https://www.seagate.com/products/enterprise-drives/exos-x/x18/
#   https://www.seagate.com/content/dam/seagate/migrated-assets/www-content/datasheets/pdfs/exos-x18-channel-DS2045-4-2106US-en_US.pdf
#
# FAN1: CPU cooler
#   ARCTIC Freezer 4U-SP5
#   https://www.arctic.de/en/Freezer-4U-SP5/ACFRE00158A
#   https://www.arctic.de/media/d2/27/24/1731948520/Spec_Sheet_Freezer_4U_SP5_EN.pdf
#
# FAN2: Inflow-Front (1x 140mm; top)
# FAN3: Inflow-Front (2x 140mm; middle, bottom)
# FAN4: Outflow-Rear (1x 120mm) + Outflow-Top (2x 140mm; back, middle)
#   ARCTIC P14 Pro PST
#   https://www.arctic.de/en/P14-Pro-PST/ACFAN00314A
#   https://www.arctic.de/media/8b/c2/0c/1753621758/Spec_Sheet_P14_Pro_PST_CO_EN.pdf
#
# FANA: Inflow-Side (3x 120mm slim)
# FANB: [unused]
#   ARCTIC P12 Slim PWM PST
#   https://www.arctic.de/en/P12-Slim-PWM-PST/ACFAN00187A
#   https://www.arctic.de/media/2b/83/fd/1690274238/Spec_Sheet_P12_Slim_PWM_PST_EN.pdf

set -e

echo "Configuring IPMI sensor thresholds..."
echo ""

# GPU Temperature Thresholds
# NVIDIA RTX 5090 thermal limits (per nvidia-smi, dynamic based on power headroom):
#   Throttle begins: ~87-89C
#   Hardware shutdown: ~92C
# IPMI reads GPU temp via SMBus external thermal diode (may differ ~3C from nvidia-smi)
#
# Default values:
#   unc (upper non-critical): na (not set)
#   ucr (upper critical):     92
#   unr (upper non-recoverable): 94
#
# New values tuned for RTX 5090 (normal operating temps up to ~87C):
#   ucr: 91 - critical alert before hardware limit
#   unr: 93 - emergency threshold
# Note: unc (upper non-critical) not supported by GPU sensors on this board

# H13SSL-N has 5 PCIe slots (3x16, 2x8)
GPU_SLOTS="GPU1 GPU2 GPU3 GPU4 GPU5"

echo "Setting GPU temperature thresholds..."
for slot in $GPU_SLOTS; do
    sensor="${slot} Temp"
    ipmitool sensor get "$sensor" >/dev/null 2>&1 || continue
    ipmitool sensor thresh "$sensor" ucr 91
    ipmitool sensor thresh "$sensor" unr 93
    echo "  $sensor: ucr=91 unr=93"
done

# Fan Thresholds
# Default values:
#   lnc (lower non-critical): na
#   lcr (lower critical): 420 RPM
#   lnr (lower non-recoverable): na
#
# New values:
#   lnc: 0, lcr: 0, lnr: 0 - disable all low RPM alerts
#
# This prevents false alarms when fans spin down at idle

# H13SSL-N fan headers
FANS="FAN1 FAN2 FAN3 FAN4 FANA FANB"

echo "Setting fan thresholds..."
for fan in $FANS; do
    ipmitool sensor get "$fan" >/dev/null 2>&1 || continue
    ipmitool sensor thresh "$fan" lnr 0 lcr 0 lnc 0 >/dev/null 2>&1 || true
    echo "  $fan: lnr=0 lcr=0 lnc=0"
done

echo "Done."

# =============================================================================
# CPU/VRM Temperature Thresholds (commented out - using factory defaults)
# =============================================================================
# AMD EPYC 9555 (Turin/Zen 5) on Supermicro H13SSL-N
# Tjmax not published by AMD, but Supermicro factory defaults are likely correct.
#
# Factory defaults:
#   CPU Temp:       ucr=100, unr=na
#   CPU_VRM0 Temp:  ucr=100, unr=na
#   CPU_VRM1 Temp:  ucr=100, unr=na
#   SOC_VRM Temp:   ucr=100, unr=na
#   VDDIO_VRM Temp: ucr=100, unr=na
#
# Uncomment below to add early warning thresholds:
#
# echo "Setting CPU/VRM temperature thresholds..."
# for sensor in "CPU Temp" "CPU_VRM0 Temp" "CPU_VRM1 Temp" "SOC_VRM Temp" "VDDIO_VRM Temp"; do
#     ipmitool sensor thresh "$sensor" unc 90 ucr 100
#     echo "  $sensor: unc=90 ucr=100"
# done

# =============================================================================
# Memory Temperature Thresholds (commented out - using factory defaults)
# =============================================================================
# DDR5 is rated up to 85C. Factory default ucr=85 is appropriate.
#
# Factory defaults:
#   DIMMA~F Temp:   ucr=85, unr=na (DDR5 DIMMs, slots A-F)
#   DIMMG~L Temp:   ucr=85, unr=na (DDR5 DIMMs, slots G-L)
#
# Uncomment below to add early warning thresholds:
#
# echo "Setting memory temperature thresholds..."
# for sensor in "DIMMA~F Temp" "DIMMG~L Temp"; do
#     ipmitool sensor get "$sensor" >/dev/null 2>&1 || continue
#     ipmitool sensor thresh "$sensor" unc 75 ucr 85
#     echo "  $sensor: unc=75 ucr=85"
# done

# =============================================================================
# Chassis Temperature Thresholds (commented out - using factory defaults)
# =============================================================================
# Factory defaults:
#   System Temp:      ucr=85, unr=na
#   Peripheral Temp:  ucr=85, unr=na
#   Inlet Temp:       ucr=na, unr=na (may show "no reading" without sensor)
#
# Uncomment below to set thresholds:
#
# echo "Setting chassis temperature thresholds..."
# for sensor in "System Temp" "Peripheral Temp"; do
#     ipmitool sensor get "$sensor" >/dev/null 2>&1 || continue
#     ipmitool sensor thresh "$sensor" unc 70 ucr 85
#     echo "  $sensor: unc=70 ucr=85"
# done

# =============================================================================
# M.2 SSD Temperature Thresholds (commented out - no reading on this system)
# =============================================================================
# NVMe in PCIe slot, not M.2 slot, so IPMI cannot read temperature.
# Use "nvme smart-log /dev/nvme0" for NVMe temperature monitoring.
#
# Factory defaults (if M.2 slot populated):
#   M2_SSD1 Temp:   ucr=na, unr=na
#   M2_SSD2 Temp:   ucr=na, unr=na
#
# Uncomment below if M.2 slots are populated and showing readings:
#
# echo "Setting M.2 SSD temperature thresholds..."
# for sensor in "M2_SSD1 Temp" "M2_SSD2 Temp"; do
#     ipmitool sensor get "$sensor" >/dev/null 2>&1 || continue
#     reading=$(ipmitool sensor get "$sensor" 2>/dev/null | grep "Sensor Reading" | grep -v "na")
#     [ -z "$reading" ] && continue
#     ipmitool sensor thresh "$sensor" unc 65 ucr 75
#     echo "  $sensor: unc=65 ucr=75"
# done

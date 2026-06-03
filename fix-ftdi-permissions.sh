#!/usr/bin/env bash
#
# Diagnose and fix FTDI USB device permissions on the host.
# Covers ANY FTDI vendor device (VID 0403) — Adafruit FT232H, FT4232HL
# on FPGA boards (ZCU104 etc.), FT2232H breakouts, FT232R serial
# dongles, and future FTDI peripherals.
#
# Why it's vendor-wide and not FT232H-only: pyftdi enumerates the
# bus by walking every 0403:* device and trying to read string
# descriptors. If ANY FTDI device on the bus has restrictive
# permissions, pyftdi fails with "no langid (permission issue, no
# string descriptors)" — even when the FT232H you actually care
# about is already fixed.
#
# Run this OUTSIDE the docker container — udev rules live on the host.
# Safe to re-run; idempotent.
#
# Usage:
#   ./fix-ftdi-permissions.sh
#
set -euo pipefail

VID=0403
RULE_FILE=/etc/udev/rules.d/11-ftdi.rules
RULE_LINE='SUBSYSTEM=="usb", ATTRS{idVendor}=="0403", MODE="0666"'

log() { printf '[ftdi-fix] %s\n' "$*"; }
err() { printf '[ftdi-fix] ERROR: %s\n' "$*" >&2; }

# --- 1. Locate every FTDI device on the bus ---------------------------------
log "scanning /sys/bus/usb/devices for VID ${VID}:* ..."

declare -a FTDI_NODES=()
declare -a FTDI_INFOS=()

for d in /sys/bus/usb/devices/*/; do
    [[ -f "$d/idVendor"  ]] || continue
    [[ -f "$d/idProduct" ]] || continue
    [[ "$(<"$d/idVendor")"  == "$VID" ]] || continue

    pid=$(<"$d/idProduct")
    bus=$(<"$d/busnum")
    dev=$(<"$d/devnum")
    product=$(cat "$d/product" 2>/dev/null || echo "?")
    manuf=$(cat "$d/manufacturer" 2>/dev/null || echo "?")
    node=$(printf '/dev/bus/usb/%03d/%03d' "$bus" "$dev")

    if [[ ! -e "$node" ]]; then
        err "device node $node for ${VID}:${pid} does not exist; skipping"
        continue
    fi

    mode=$(stat -c '%a' "$node")
    FTDI_NODES+=("$node")
    FTDI_INFOS+=("${VID}:${pid} | ${manuf} ${product} | sysfs=$(basename "$d") | mode=${mode}")
    log "found ${VID}:${pid}  ${manuf} ${product}  → $node (mode=${mode})"
done

if [[ ${#FTDI_NODES[@]} -eq 0 ]]; then
    err "no FTDI (VID ${VID}) devices found. Is anything plugged in?"
    exit 1
fi

log "${#FTDI_NODES[@]} FTDI device(s) total"

# --- 2. Check whether all already have mode 0666 ----------------------------
need_fix=0
for node in "${FTDI_NODES[@]}"; do
    mode=$(stat -c '%a' "$node")
    [[ "$mode" == "666" ]] || need_fix=1
done

if [[ $need_fix -eq 0 ]]; then
    log "all FTDI devices already at mode 0666 — nothing to fix on the host."
    log "if it still fails inside the container, restart the container so it picks up the modes."
    exit 0
fi

# --- 3. Install / refresh the udev rule -------------------------------------
need_sudo=
[[ $EUID -ne 0 ]] && need_sudo=sudo

# Migrate any older product-specific rules into the vendor-wide form.
# Check for the line verbatim; if absent, rewrite the file with the
# vendor-only rule (which subsumes any 0403:<pid> single-product entries).
if [[ -f "$RULE_FILE" ]] && grep -qF "$RULE_LINE" "$RULE_FILE"; then
    log "udev rule already present in $RULE_FILE"
else
    log "writing vendor-wide udev rule to $RULE_FILE..."
    echo "$RULE_LINE" | $need_sudo tee "$RULE_FILE" >/dev/null
    if [[ -f "$RULE_FILE" ]]; then
        log "current rule file contents:"
        cat "$RULE_FILE" | sed 's/^/    /'
    fi
fi

log "reloading udev rules and re-triggering for all VID ${VID} devices..."
$need_sudo udevadm control --reload-rules
$need_sudo udevadm trigger --action=change \
    --subsystem-match=usb --attr-match="idVendor=$VID"

# --- 4. Re-check every device ------------------------------------------------
log "permissions after reload:"
all_ok=1
for node in "${FTDI_NODES[@]}"; do
    new_mode=$(stat -c '%a' "$node")
    if [[ "$new_mode" == "666" ]]; then
        log "  $node  mode=$new_mode  ✓"
    else
        log "  $node  mode=$new_mode  ✗"
        all_ok=0
    fi
done

if [[ $all_ok -eq 1 ]]; then
    log "SUCCESS — all FTDI devices now at mode 0666."
    log "next: restart your docker container so the new modes are visible inside."
    exit 0
fi

log "one or more devices still below 0666 — udev didn't re-apply on a 'change' event."
log "TRY: physically unplug+replug those devices, then re-run this script."
log "(udev applies new modes on 'add' events; some kernels skip 'change' for already-enumerated devices.)"
exit 2

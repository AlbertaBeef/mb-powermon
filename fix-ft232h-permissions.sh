#!/usr/bin/env bash
#
# Diagnose and fix Adafruit FT232H (0403:6014) USB permissions on the host.
# Run this OUTSIDE the docker container — udev rules live on the host.
# Safe to re-run; idempotent.
#
# Usage:
#   ./fix-ft232h-permissions.sh
#
set -euo pipefail

VID=0403
PID=6014
RULE_FILE=/etc/udev/rules.d/11-ftdi.rules
RULE_LINE='SUBSYSTEM=="usb", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6014", MODE="0666"'

log() { printf '[ft232h-fix] %s\n' "$*"; }
err() { printf '[ft232h-fix] ERROR: %s\n' "$*" >&2; }

# --- 1. Locate the FT232H ----------------------------------------------------
log "scanning /sys/bus/usb/devices for VID:PID ${VID}:${PID}..."

bus=
dev=
for d in /sys/bus/usb/devices/*/; do
    [[ -f "$d/idVendor"  ]] || continue
    [[ -f "$d/idProduct" ]] || continue
    [[ "$(<"$d/idVendor")"  == "$VID" ]] || continue
    [[ "$(<"$d/idProduct")" == "$PID" ]] || continue
    bus=$(<"$d/busnum")
    dev=$(<"$d/devnum")
    log "found FT232H at sysfs path $d (bus=$bus dev=$dev)"
    break
done

if [[ -z "$bus" ]]; then
    err "no FT232H (${VID}:${PID}) found. Is it plugged in?"
    exit 1
fi

# Pad bus/dev to 3 digits — that's how /dev/bus/usb names them.
node=$(printf '/dev/bus/usb/%03d/%03d' "$bus" "$dev")

if [[ ! -e "$node" ]]; then
    err "expected device node $node does not exist"
    exit 1
fi

log "device node: $node"
log "current permissions:"
ls -l "$node"

# --- 2. Check whether the rule is already in place ---------------------------
mode=$(stat -c '%a' "$node")
if [[ "$mode" == "666" ]]; then
    log "permissions already 0666 — nothing to fix on the host."
    log "if it still fails inside the container, restart the container so it picks up the mode."
    exit 0
fi

log "current mode is $mode (need 666 for non-root pyftdi access)"

# --- 3. Install / refresh the udev rule --------------------------------------
need_sudo=
[[ $EUID -ne 0 ]] && need_sudo=sudo

if [[ -f "$RULE_FILE" ]] && grep -qF "$RULE_LINE" "$RULE_FILE"; then
    log "udev rule already present in $RULE_FILE"
else
    log "writing udev rule to $RULE_FILE..."
    echo "$RULE_LINE" | $need_sudo tee "$RULE_FILE" >/dev/null
fi

log "reloading udev rules and re-triggering..."
$need_sudo udevadm control --reload-rules
$need_sudo udevadm trigger --action=change \
    --subsystem-match=usb --attr-match="idVendor=$VID" --attr-match="idProduct=$PID"

# --- 4. Re-check -------------------------------------------------------------
log "permissions after reload:"
ls -l "$node"

new_mode=$(stat -c '%a' "$node")
if [[ "$new_mode" == "666" ]]; then
    log "SUCCESS — mode is now 0666."
    log "next: restart your docker container so the new permission is visible inside."
else
    log "mode is still $new_mode — udev didn't re-apply on a 'change' event."
    log "TRY: physically unplug and replug the FT232H, then re-run this script."
    log "(udev applies new modes on 'add' events; some kernels skip 'change' for already-enumerated devices.)"
    exit 2
fi

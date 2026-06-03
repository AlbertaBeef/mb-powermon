#!/usr/bin/env python3
"""NVTOP-style TUI monitor for AI accelerators (Hailo + Axelera + PMD2 + INA228).

Curses-based terminal monitor for Hailo-8/8L, Axelera Metis,
ElmorLabs PMD2, and Adafruit INA228 devices. Shows per-device
identity, PCIe link width/speed/ASPM (from sysfs), temperatures, and
power, with a scrolling time-series graph for each metric. Runs
anywhere a terminal does — no X server, no OpenCV window.

Inspired by NVTOP (https://github.com/Syllo/nvtop) and adapted from
ai_nvtop.py.

Data sources
------------
- PCIe link:    /sys/bus/pci/devices/<BDF>/{current,max}_link_{width,speed}
- ASPM:         PCI Express Capability's Link Control register, read
                from /sys/.../config (needs root or passwordless sudo
                for the byte range past the PCI header).
- Hailo-8:      hailo_platform Device.control API
                  - chip temperature via get_chip_temperature()
                  - power via get_power_measurement() (firmware-side
                    averaging of 256 INA231 samples)
- Axelera Metis: axelera.runtime Context.list_devices() (board info)
                + triton_trace --slog --peek (per-core temperatures).
                Power is not exposed on Metis M.2 so its trace stays
                empty.
- DeepX M1:     dxrt-cli -s parses per-NPU voltage/clock/temperature
                from the DXRT runtime CLI (the dx_engine Python SDK
                doesn't expose telemetry). 3 NPU cores → T0/T1/T2.
                Power is not exposed on M1 so its trace stays empty.
- MemryX MX3:   per-MPU temperatures from Linux hwmon (the
                memx_pcie_ai_chip driver registers
                /sys/class/hwmon/hwmonN with name='memx0' and
                temp1..temp16 inputs in millidegrees C). On the
                4-chip MX3-2280-M-4 SKU, temp1..temp4 populate one
                per MPU; on a 2-chip SKU only temp1..temp2. Power
                is not exposed via hwmon (use INA228 for ground-
                truth power), so the POW trace stays empty.
- ElmorLabs PMD2: USB CDC (VID:PID 0483:5740) via pyserial.
                Reads total power and individual rails
                (ATX12V, ATX5V, ATX5VSB, ATX3.3V, EPS, HPWR, PCIE2/3).
                Displays total and PCIE1/2/3 rails.
- Adafruit:     1 to 4 INA228 high-precision power monitors over an
                Adafruit FT232H breakout (USB→MPSSE I2C bridge,
                VID:PID 0403:6014). Scans the canonical INA228
                addresses {0x40, 0x41, 0x44, 0x45} and creates one
                POW (W) trace per responding sensor; per-sensor
                bus voltage and current are tracked but only graphed
                in --once / --csv to keep the panel readable.
                Calibration (shunt Ω, full-scale A, address list)
                is CLI-overridable and shared across sensors.
                Single-instance — Blinka's BLINKA_FT232H mode picks
                the first FT232H on the host.

Exclusivity caveat
------------------
Each vendor SDK holds an exclusive handle to a device while inference
is running. If another process owns a device, this tool can still
read sysfs PCIe info but the temp/power columns will show "ERROR".

Controls
--------
  q / ESC  quit
  r        reset history buffers
  h        toggle help overlay
"""

import argparse
import contextlib
import curses
import glob
import importlib.util
import locale
import os
import re
import struct
import subprocess
import sys
import time
from collections import deque, namedtuple
from datetime import datetime


# Tell Adafruit Blinka to use the FT232H USB→I2C backend BEFORE any
# Blinka-touching module is imported. Blinka caches the platform
# decision the first time `digitalio` / `board` / friends load — and
# `adafruit_ina228` transitively imports `digitalio` via
# `adafruit_bus_device`, so even our install-check import would lock
# in the wrong backend if the env var weren't set yet. Setting it
# unconditionally costs nothing on hosts that never touch Blinka.
os.environ.setdefault("BLINKA_FT232H", "1")


# A renderable value tracked by a probe. Each probe declares one or
# more of these in its `metrics` list; the TUI iterates the list to
# build the inline-bar row and the graph traces. Decoupling probes
# from the old hardcoded TEMP+POW pair lets PMD2 expose three power
# rails (PCIE1/2/3) plus a TOTAL on a different axis without
# special-casing the renderer.
#
#   label    : short string shown as the bar label (max ~6 chars)
#   history  : deque of float|None samples, newest at the right
#   unit     : "°C" or "W" — also chosen by the TUI to pick the
#              right --temp-max / --power-max axis cap
#   color_id : CP_* color-pair id used for both the bar label and
#              the graph trace, so the eye can map them at a glance
#   max_val  : optional per-metric axis cap. None means "fall back
#              to the unit-based default" (TEMP_MAX / POWER_MAX).
#              Use it when a metric needs a different scale from
#              its peers — e.g. PMD2 TOTAL on a 200W axis while
#              PCIE1/2/3 share the standard 10W axis.
Metric = namedtuple("Metric", "label history unit color_id max_val",
                    defaults=[None])


# Curses color-pair ids. Defined at module scope (rather than near
# _init_colors) so probe classes can reference them when building
# their `metrics` list at construction time, before curses is up.
CP_TITLE = 1        # top status bar (black on cyan)
CP_OK = 2           # green — normal values
CP_WARN = 3         # yellow — warning values
CP_CRIT = 4         # red — critical values
CP_DIM = 5          # white dim — axis labels, borders
CP_ACCENT = 6       # cyan — pcie link ok
CP_TRACE_TEMP = 7   # temperature trace (yellow, nvtop's "GPU%" color)
CP_TRACE_POWER = 8  # power trace (cyan, nvtop's "mem%" color)
CP_FOOTER = 9       # bottom bar (black on green, nvtop-style)
CP_TRACE_TOTAL = 10 # PMD2 TOTAL trace (magenta — distinct from rails)


@contextlib.contextmanager
def _silence_fd_output():
    """Redirect C-level stdout/stderr (fd 1/2) to /dev/null.

    HailoRT's C library logs warnings (e.g. the overcurrent-protection
    notice on every start_power_measurement) directly to stderr,
    bypassing Python and curses. Under curses those writes corrupt the
    screen, so we swap the fds to /dev/null for the duration of noisy
    calls. Temperature reads are silent; only the power-measurement
    start path needs this.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        yield
        return
    saved_out = os.dup(1)
    saved_err = os.dup(2)
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        os.close(devnull)


HAILO_PCI_VENDOR = "0x1e60"
AXELERA_PCI_VENDOR = "0x1f9d"
# DeepX (DEEPX Co., Ltd.) — vendor id 0x1ff4. Not yet in the public
# PCI registry at firmware 2.5.0 / DXRT 3.2.0; matched by raw id rather
# than by the "DEEPX" string in lspci output.
DEEPX_PCI_VENDOR = "0x1ff4"
# MemryX MX3 — the memx_pcie_ai_chip driver registers a hwmon node
# named "memx0". We scan via hwmon name (canonical telemetry path)
# rather than PCI vendor ID, because (a) the vendor ID isn't yet in
# the public PCI registry and (b) hwmon presence implies the driver
# is loaded, which is what actually matters for telemetry.
MEMRYX_HWMON_NAME = "memx0"
# Per-board vendor sysfs node `/sys/memx<N>/temperature`. Format is
# human-readable, one line per chip:
#   CHIP(0) PVT3 Temperature: Temperature: 62 C (335 K) (ThermalThrottlingState: 0)
# We parse the ThermalThrottlingState per chip — the SDK's
# `mxa.get_thermal_state(device_id)` is module-level (no chip_id arg)
# and effectively the OR of these, so this is the only source for
# per-chip granularity. Always available when memx_pcie_ai_chip is
# loaded; no SDK dependency.
MEMRYX_SYSFS_FMT = "/sys/memx{}/temperature"
MEMRYX_SYSFS_RE = re.compile(
    r"^\s*CHIP\(\s*(\d+)\s*\).*?ThermalThrottlingState:\s*(\d+)",
    re.MULTILINE)
SYSFS_PCI = "/sys/bus/pci/devices"
SYSFS_HWMON = "/sys/class/hwmon"


# ---------------------------------------------------------------------------
# sysfs helpers (vendor-agnostic, work even when hailo_platform is absent)
# ---------------------------------------------------------------------------

def _read_sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_aspm_sysfs(bdf):
    """ASPM enable bits from /sys/bus/pci/devices/<BDF>/link/{l0s,l1}_aspm.

    Many kernels build with these knobs disabled, leaving link/ empty
    or absent — caller should fall back to lspci in that case.
    """
    base = os.path.join(SYSFS_PCI, bdf, "link")
    if not os.path.isdir(base):
        return None
    enabled = []
    any_present = False
    for fname, label in (("l0s_aspm", "L0s"), ("l1_aspm", "L1")):
        v = _read_sysfs(os.path.join(base, fname))
        if not v:
            continue
        any_present = True
        if v == "1":
            enabled.append(label)
    if not any_present:
        return None
    return "+".join(enabled) if enabled else "off"


def _read_aspm_lspci(bdf):
    """Parse ASPM control out of `lspci -vv -s <bdf>`.

    Looks for the PCI Express Capability's LnkCtl line, e.g.
        LnkCtl: ASPM L1 Enabled; ...
        LnkCtl: ASPM Disabled; ...
    Some lspci builds emit "ASPM L0s L1" (space-separated). Returns
    a "+"-joined string of the enabled substates, "off" when ASPM is
    explicitly disabled, or None when lspci isn't usable.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["lspci", "-vv", "-s", bdf],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    for line in out.splitlines():
        s = line.strip()
        if not s.startswith("LnkCtl:"):
            continue
        seg = s.split(";", 1)[0]
        if "ASPM Disabled" in seg:
            return "off"
        idx = seg.find("ASPM ")
        if idx < 0:
            continue
        rest = seg[idx + len("ASPM "):]
        rest = rest.split("Enabled")[0].strip().rstrip(",")
        if not rest:
            continue
        return "+".join(rest.split())
    return None


def _read_pci_config(bdf, want_bytes=256):
    """Read PCI config space for a device, escalating via sudo if needed.

    Direct read of /sys/bus/pci/devices/<BDF>/config returns the full
    256-byte standard config space when the caller is root, but is
    truncated to the 64-byte PCI header for unprivileged users. When
    we get a short read and aren't root, try `sudo -n cat` once: this
    works transparently for accounts with passwordless sudo (typical
    on dev workstations) and fails fast otherwise.
    """
    path = os.path.join(SYSFS_PCI, bdf, "config")
    try:
        with open(path, "rb") as f:
            cfg = f.read(want_bytes)
    except OSError:
        return None
    if len(cfg) >= want_bytes or os.geteuid() == 0:
        return cfg
    import subprocess
    try:
        out = subprocess.run(
            ["sudo", "-n", "cat", path],
            capture_output=True, timeout=2, stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return cfg
    if out.returncode == 0 and len(out.stdout) >= want_bytes:
        return out.stdout[:want_bytes]
    return cfg


def _read_aspm_config(bdf):
    """Read ASPM control directly from PCI config space.

    Walks the capability list in /sys/bus/pci/devices/<BDF>/config to
    find the PCI Express Capability (id 0x10), whose Link Control
    register's low 2 bits encode the active ASPM mode:

        0b00 = Disabled, 0b01 = L0s, 0b10 = L1, 0b11 = L0s+L1.

    Caveat: the kernel restricts non-root reads of this file to the
    first 64 bytes (PCI header). _read_pci_config() will try a one-
    shot `sudo -n cat` to recover the full 256 bytes; if neither
    works the function returns None.
    """
    cfg = _read_pci_config(bdf)
    if cfg is None or len(cfg) < 0x40:
        return None
    cap_ptr = cfg[0x34] & 0xfc
    seen = set()
    while cap_ptr and cap_ptr not in seen and cap_ptr + 4 <= len(cfg):
        seen.add(cap_ptr)
        cap_id = cfg[cap_ptr]
        if cap_id == 0x10:  # PCI Express Capability
            if cap_ptr + 0x12 > len(cfg):
                return None
            link_ctl = cfg[cap_ptr + 0x10] | (cfg[cap_ptr + 0x11] << 8)
            return {0: "off", 1: "L0s", 2: "L1", 3: "L0s+L1"}[link_ctl & 0x3]
        cap_ptr = cfg[cap_ptr + 1] & 0xfc
    return None


def _read_aspm(bdf):
    """Return (state, error) for a device's ASPM configuration.

    On success: state is "L0s+L1"/"L1"/"L0s"/"off", error is None.
    On failure: state is None, error is a short reason. The most
    common failure for an unprivileged user is the 64-byte config
    cap; the message hints at both fixes (root or passwordless sudo).
    """
    for fn in (_read_aspm_config, _read_aspm_sysfs, _read_aspm_lspci):
        val = fn(bdf)
        if val is not None:
            return val, None
    if os.geteuid() != 0:
        return None, "needs root or sudo -n"
    return None, None




def pcie_info(bdf):
    """Return dict of PCIe link info for a BDF, or None if the path is gone."""
    base = os.path.join(SYSFS_PCI, bdf)
    if not os.path.isdir(base):
        return None
    aspm, aspm_err = _read_aspm(bdf)
    return {
        "bdf": bdf,
        "vendor": _read_sysfs(os.path.join(base, "vendor")),
        "device": _read_sysfs(os.path.join(base, "device")),
        "current_link_width": _read_sysfs(
            os.path.join(base, "current_link_width")),
        "max_link_width": _read_sysfs(
            os.path.join(base, "max_link_width")),
        "current_link_speed": _read_sysfs(
            os.path.join(base, "current_link_speed")),
        "max_link_speed": _read_sysfs(
            os.path.join(base, "max_link_speed")),
        "aspm": aspm,
        "aspm_error": aspm_err,
    }


def scan_sysfs_hailo():
    """Return sorted list of BDFs whose PCI vendor id matches Hailo."""
    found = []
    for path in sorted(glob.glob(os.path.join(SYSFS_PCI, "*"))):
        vendor = _read_sysfs(os.path.join(path, "vendor"))
        if vendor and vendor.lower() == HAILO_PCI_VENDOR:
            found.append(os.path.basename(path))
    return found


def scan_hailo_devices():
    """Return list of Hailo BDFs. Prefers hailo_platform, falls back to sysfs."""
    try:
        from hailo_platform import Device
        ids = list(Device.scan())
        if ids:
            return ids
    except Exception:
        pass
    return scan_sysfs_hailo()


def scan_sysfs_axelera():
    """Return sorted list of BDFs whose PCI vendor id matches Axelera."""
    found = []
    for path in sorted(glob.glob(os.path.join(SYSFS_PCI, "*"))):
        vendor = _read_sysfs(os.path.join(path, "vendor"))
        if vendor and vendor.lower() == AXELERA_PCI_VENDOR:
            found.append(os.path.basename(path))
    return found


def scan_axelera_devices():
    """Return list of (bdf, sdk_device_or_None) pairs for Axelera Metis.

    Pairs are formed by index after sorting both lists naturally,
    which works for the single-card case typical on dev hosts. The
    SDK's `name` (e.g. "metis-0:3:0") doesn't directly match the
    kernel BDF format ("0000:01:00.0"), so a strict mapping isn't
    attempted — the per-device panel still shows the BDF for
    disambiguation when multiple cards are installed.
    """
    bdfs = scan_sysfs_axelera()
    sdk_devices = []
    try:
        from axelera.runtime import objects as axr
        sdk_devices = list(axr.Context().list_devices())
    except Exception:
        pass
    pairs = []
    for i, bdf in enumerate(bdfs):
        sdk = sdk_devices[i] if i < len(sdk_devices) else None
        pairs.append((bdf, sdk))
    return pairs


_TRITON_TRACE_CACHED = False
_TRITON_TRACE_PATH = None


def _find_triton_trace():
    """Locate the triton_trace binary (used to peek Metis core temps).

    Cached after first call so we don't re-glob /opt on every probe.
    Returns None when neither $PATH nor the standard install prefix
    /opt/axelera/runtime-*/bin contains it.
    """
    global _TRITON_TRACE_CACHED, _TRITON_TRACE_PATH
    if _TRITON_TRACE_CACHED:
        return _TRITON_TRACE_PATH
    _TRITON_TRACE_CACHED = True
    import shutil
    path = shutil.which("triton_trace")
    if path:
        _TRITON_TRACE_PATH = path
        return path
    matches = sorted(glob.glob("/opt/axelera/runtime-*/bin/triton_trace"))
    if matches:
        _TRITON_TRACE_PATH = matches[-1]  # latest version wins
    return _TRITON_TRACE_PATH


# axelera.runtime.BoardType enum -> short architecture label, used as
# the bracket-suffix on line 1 (mirrors how we render Hailo's "HAILO8"
# arch tag). Falls back to UPPERCASE of the raw enum name.
_AXELERA_BOARD_ARCH = {
    "alpha_pcie": "α-PCIe",
    "alpha_m2":   "α-M.2",
    "pcie":       "PCIe",
    "m2":         "M.2",
    "devboard":   "DevBoard",
    "sbc":        "SBC",
}


def _axelera_board_arch(board_type):
    """Stringify an axelera.runtime.BoardType enum -> short label."""
    if board_type is None:
        return ""
    s = str(board_type).split(".")[-1].lower()
    return _AXELERA_BOARD_ARCH.get(s, s.upper() or "")


def scan_sysfs_deepx():
    """Return sorted list of BDFs whose PCI vendor id matches DeepX."""
    found = []
    for path in sorted(glob.glob(os.path.join(SYSFS_PCI, "*"))):
        vendor = _read_sysfs(os.path.join(path, "vendor"))
        if vendor and vendor.lower() == DEEPX_PCI_VENDOR:
            found.append(os.path.basename(path))
    return found


def scan_deepx_devices():
    """Return list of DeepX BDFs (single-device per host typical)."""
    return scan_sysfs_deepx()


def scan_sysfs_memryx():
    """Return sorted list of (hwmon_path, bdf) pairs for MemryX devices.

    Scans /sys/class/hwmon/hwmon* for entries whose `name` matches
    MEMRYX_HWMON_NAME ("memx0"). For each match, resolves the BDF
    via the `device` symlink. This dual return lets the probe skip a
    second hwmon search at __init__ time — it already has the path.

    Driver presence implies the device is enumerated AND usable
    (PCI vendor match alone would also report devices whose driver
    failed to load).
    """
    found = []
    for d in sorted(glob.glob(os.path.join(SYSFS_HWMON, "hwmon*"))):
        try:
            with open(os.path.join(d, "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        if name != MEMRYX_HWMON_NAME:
            continue
        dev_link = os.path.join(d, "device")
        if not os.path.islink(dev_link):
            continue
        try:
            target = os.readlink(dev_link)
        except OSError:
            continue
        bdf = os.path.basename(target)
        found.append((d, bdf))
    return sorted(found, key=lambda x: x[1])


def scan_memryx_devices():
    """Return list of MemryX BDFs (drops the hwmon path; MemryXProbe
    rediscovers it from the BDF at __init__ time)."""
    return [bdf for (_, bdf) in scan_sysfs_memryx()]


_DXRT_CLI_CACHED = False
_DXRT_CLI_PATH = None


def _find_dxrt_cli():
    """Locate the dxrt-cli binary (DeepX runtime telemetry CLI).

    Cached after first call. Checks $PATH first, then falls back to
    /usr/local/bin/dxrt-cli (the canonical install location). Returns
    None if neither resolves.
    """
    global _DXRT_CLI_CACHED, _DXRT_CLI_PATH
    if _DXRT_CLI_CACHED:
        return _DXRT_CLI_PATH
    _DXRT_CLI_CACHED = True
    import shutil
    path = shutil.which("dxrt-cli")
    if path:
        _DXRT_CLI_PATH = path
        return path
    cand = "/usr/local/bin/dxrt-cli"
    if os.path.exists(cand) and os.access(cand, os.X_OK):
        _DXRT_CLI_PATH = cand
    return _DXRT_CLI_PATH


# ElmorLabs PMD2 — USB CDC measurement device. STM32 VID:PID,
# 115200 baud, binary protocol with single-byte commands.
PMD2_USB_VID = 0x0483
PMD2_USB_PID = 0x5740


def scan_pmd2_devices():
    """Return sorted list of serial port paths for connected PMD2s.

    Uses pyserial's port enumeration filtered by USB VID:PID. Returns
    [] if pyserial isn't installed or no PMD2 is connected.
    """
    try:
        import serial.tools.list_ports
    except ImportError:
        return []
    found = []
    for p in serial.tools.list_ports.comports():
        if p.vid == PMD2_USB_VID and p.pid == PMD2_USB_PID:
            found.append(p.device)
    return sorted(found)


# Adafruit FT232H breakout — USB→I2C bridge (FTDI VID:PID). The chip
# also enumerates as 0403:6011/6010/6015 in different modes, but the
# Adafruit breakout ships in MPSSE mode at 6014.
FT232H_USB_VID = 0x0403
FT232H_USB_PID = 0x6014


def _ft232h_present_in_sysfs():
    """True iff a 0403:6014 device is enumerated under /sys/bus/usb."""
    base = "/sys/bus/usb/devices"
    if not os.path.isdir(base):
        return False
    for entry in os.listdir(base):
        d = os.path.join(base, entry)
        try:
            with open(os.path.join(d, "idVendor")) as f:
                vid = int(f.read().strip(), 16)
            with open(os.path.join(d, "idProduct")) as f:
                pid = int(f.read().strip(), 16)
        except (OSError, ValueError):
            continue
        if vid == FT232H_USB_VID and pid == FT232H_USB_PID:
            return True
    return False


def scan_adafruit_devices():
    """Return ['adafruit-ft232h'] when an Adafruit FT232H is usable, else [].

    Cheap presence check only — does NOT actually claim the FT232H,
    which would block other Blinka clients. The real I2C-bus scan
    (and per-INA228 instantiation) happens in INA228Probe._open()
    once we've decided to instantiate. We require BOTH the FT232H USB
    device to be enumerated AND `adafruit_ina228` to be importable;
    either alone is insufficient. Single-instance: Blinka's
    BLINKA_FT232H mode picks the first FT232H on the host.
    """
    if not _ft232h_present_in_sysfs():
        return []
    if importlib.util.find_spec("adafruit_ina228") is None:
        return []
    return ["adafruit-ft232h"]


# ---------------------------------------------------------------------------
# Per-device probe: opens a Device handle and polls telemetry
# ---------------------------------------------------------------------------

class HailoProbe:
    """Owns a single Hailo Device handle and polls temp/power at ~1Hz.

    Two device-acquisition modes:
      1. Standalone (sdk_device=None): opens its own Device(bdf). This
         is the default for the mb-powermon TUI.
      2. Borrow (sdk_device=<Device>): uses a caller-provided handle.
         Intended for embedded usage where another component
         (e.g. an HailoInference VDevice) already owns the Device for
         inference and wants to read telemetry through the same
         handle to avoid contending for the firmware-side
         power-measurement buffer. The probe still manages its own
         start_power_measurement / stop_power_measurement on the
         borrowed handle; it just doesn't release the handle on
         close().
    """

    HISTORY_MAX = 720  # ~12 min at 1Hz (default; overridable via __init__)

    def __init__(self, bdf, sdk_device=None, history_max=None):
        self.bdf = bdf
        self.pcie = pcie_info(bdf) or {}
        self.identity = {}
        self.device = None
        self.power_started = False
        self.error = None
        self.history_max = history_max or self.HISTORY_MAX
        # Track whether we opened the Device ourselves vs borrowed it
        # from the caller. In borrow mode we skip Device(bdf) entirely
        # and don't try to release the handle on close().
        self._borrowed_device = False

        # When another HailoRT client (e.g. `hailortcli benchmark`)
        # calls start_power_measurement() on the same firmware buffer,
        # our averaging session gets clobbered — subsequent reads
        # return 0 or raise. Count consecutive "bad" polls and try to
        # re-`start_power_measurement()` to recover. Not applicable in
        # borrow mode (the caller controls the handle, no external
        # client can race for it).
        self._power_fail_count = 0
        self._power_restart_threshold = 3  # ~3s at 1Hz

        # Hailo-8 has two on-die temperature sensors (TS0/TS1) — expose
        # them as separate metrics so both readings show on the panel.
        self.history_ts0 = deque(maxlen=self.history_max)
        self.history_ts1 = deque(maxlen=self.history_max)
        self.history_power = deque(maxlen=self.history_max)
        # POW listed first so it occupies the leftmost inline bar AND
        # paints last in the graph (renderer iterates metrics in
        # reverse for z-order), keeping the power trace visible above
        # the temperature traces at crossings. TS1 uses magenta to
        # distinguish from TS0's yellow on the same panel.
        self.metrics = [
            Metric("POW", self.history_power, "W",  CP_TRACE_POWER),
            Metric("TS0", self.history_ts0,   "°C", CP_TRACE_TEMP),
            Metric("TS1", self.history_ts1,   "°C", CP_TRACE_TOTAL),
        ]

        # Throttling state from device.control._get_health_information().
        # Four flags + a CLOCK prefix (set as state_flags below after
        # _open() confirms the call works on this firmware):
        #   THERMAL   — current_temperature_throttling_level (5 states:
        #               -1 / 0 / 1 / 2 / 3 → OK / L1 / L2 / L3 / MAX),
        #               custom formatter maps each to a distinct color
        #               (green → yellow → magenta → red → red+reverse).
        #   TEMP_PROT — temperature_throttling_active (feature-enable
        #               bit, NOT a "has-fired" latch; healthy idle
        #               reads 1=ON). Uses _on_off_badge.
        #   OCP       — current_overcurrent_zone (binary GREEN=0/RED=1
        #               per OvercurrentAlertState enum; firmware-
        #               internal 3-level + max structure isn't lifted
        #               into Python). Default OK/ALERT formatter.
        #   OCP_PROT  — overcurrent_throttling_active (feature-enable
        #               bit, same semantics as TEMP_PROT). _on_off_badge.
        # CLOCK prefix: derived per poll from THERMAL level + the
        # firmware's requested clock & per-level throttle table
        # (captured once at _probe_health_info()).
        self._hist_thermal_throttle = deque(maxlen=self.history_max)
        self._hist_temp_prot        = deque(maxlen=self.history_max)
        self._hist_ocp_zone         = deque(maxlen=self.history_max)
        self._hist_ocp_prot         = deque(maxlen=self.history_max)
        self._requested_clock_mhz   = None
        self._throttle_level_freq_mhz = {}
        self._current_clock_mhz     = None
        self._health_supported      = False  # set in _open() if probe succeeds
        self.state_flags = []   # populated in _open()

        self._open(sdk_device)

    def _open(self, sdk_device=None):
        if sdk_device is None:
            try:
                from hailo_platform import Device
            except ImportError as e:
                self.error = f"hailo_platform not installed: {e}"
                return
            try:
                self.device = Device(self.bdf)
            except Exception as e:
                msg = str(e).strip() or type(e).__name__
                self.error = f"{msg[:60]}"
                self.device = None
                return
        else:
            self.device = sdk_device
            self._borrowed_device = True
        try:
            info = self.device.control.identify()
            self.identity = {
                "board_name": _clean(getattr(info, "board_name", None)),
                "part_number": _clean(getattr(info, "part_number", None)),
                "product_name": _clean(getattr(info, "product_name", None)),
                "serial": _clean(getattr(info, "serial_number", None)),
                "arch": getattr(info, "device_architecture", None),
                "fw_version": getattr(info, "fw_version", None),
            }
            # Decode the part-number key suffix into a short description.
            # Hailo-8 M.2 part numbers encode the connector keying at
            # position 4: HM218M..=M-Key, HM218B..=B+M-Key, HM218A..=A+E-Key.
            part = self.identity.get("part_number") or ""
            key_map = {
                "M": "Hailo-8 M.2 M-Key module",
                "B": "Hailo-8 M.2 B+M-Key module",
                "A": "Hailo-8 M.2 A+E-Key module",
            }
            self.identity["description"] = (
                key_map.get(part[4], "")
                if len(part) >= 5 and part.startswith("HM") else "")
            self._start_power()
            self._probe_health_info()
        except Exception as e:
            msg = str(e).strip() or type(e).__name__
            self.error = f"{msg[:60]}"
            self.device = None

    def _probe_health_info(self):
        """One-shot capability check + state_flags wiring + clock-table
        capture.

        The throttling-state API is on `control._get_health_information`
        (underscore-prefixed in HailoRT 2025-10's pyhailort; the
        non-underscore alias was renamed). If the call succeeds we
        wire four state_flags entries (THERMAL/TEMP_PROT/OCP/OCP_PROT)
        and capture the firmware's `requested_*_clock_freq` + per-
        level throttle table so `state_prefix` can render the current
        effective clock as `CLOCK <freq>MHz` ahead of the badges.
        """
        if self.device is None:
            return
        ctrl = self.device.control
        getter = getattr(ctrl, "_get_health_information", None) \
                 or getattr(ctrl, "get_health_information", None)
        if getter is None:
            return
        try:
            h = getter()
        except Exception:
            return
        self._health_supported = True
        # Capture the clock context once — these don't change at runtime.
        req = getattr(h, "requested_temperature_clock_freq", None) \
              or getattr(h, "requested_overcurrent_clock_freq", None)
        if req:
            self._requested_clock_mhz = req / 1e6
        # Map throttle level (`level_number`) → effective NN clock (MHz).
        for lvl in (getattr(h, "temperature_throttling_levels", None) or []):
            f = getattr(lvl, "throttling_nn_clock_freq", None)
            n = getattr(lvl, "level_number", None)
            if n is not None and f:
                self._throttle_level_freq_mhz[int(n)] = f / 1e6
        # Order: thermal first (its level drives CLOCK), then temp-
        # protection-enable, then OCP zone + its protection-enable.
        # TEMP_PROT and OCP_PROT use the 4-tuple "plain" form: white
        # text (`_on_off_white_badge`), no brackets, no bold — they
        # nearly always read ON at idle and the rare OFF transition
        # is easier to spot as inline plain text than as a loud
        # bracketed badge that visually competes with the more
        # interesting THERMAL / OCP indicators.
        self.state_flags = [
            ("THERMAL",   self._hist_thermal_throttle, _hailo_thermal_badge),
            ("TEMP_PROT", self._hist_temp_prot,        _on_off_white_badge, True),
            ("OCP",       self._hist_ocp_zone),
            ("OCP_PROT",  self._hist_ocp_prot,         _on_off_white_badge, True),
        ]

    @property
    def state_prefix(self):
        """`(label, value_str)` rendered ahead of the state-flag badges
        on the status row. For Hailo: current effective NN clock in
        MHz — `requested_*_clock_freq` (typically 400 MHz) at level
        -1, or the throttled per-level frequency when throttling is
        active (350/300/250/200 MHz on this firmware).

        Returns None if the health probe hasn't run successfully or
        the clock context is missing — the TUI then omits the prefix
        and the row starts with the first badge.
        """
        if self._current_clock_mhz is None:
            return None
        return ("CLOCK", f"{self._current_clock_mhz:.0f}MHz")

    def _start_power(self):
        """(Re)start firmware-side averaged power measurement.

        Calling this while a session is already active (ours or another
        client's) overwrites it, which is exactly what we want when
        recovering from a clobbered session.  The call emits a HailoRT
        overcurrent-protection warning to stderr every time — we
        silence it at the fd level so curses output stays clean.
        """
        if self.device is None:
            return False
        try:
            from hailo_platform import (
                MeasurementBufferIndex, AveragingFactor, SamplingPeriod,
            )
            ctrl = self.device.control
            with _silence_fd_output():
                try:
                    ctrl.stop_power_measurement()
                except Exception:
                    pass
                ctrl.set_power_measurement(
                    buffer_index=(
                        MeasurementBufferIndex.MEASUREMENT_BUFFER_INDEX_0))
                ctrl.start_power_measurement(
                    averaging_factor=AveragingFactor.AVERAGE_256,
                    sampling_period=SamplingPeriod.PERIOD_1100us)
            self.power_started = True
            self._power_fail_count = 0
            return True
        except Exception:
            self.power_started = False
            return False

    def poll(self):
        """Sample one (ts0, ts1, power) tuple and append to history."""
        ts0 = None
        ts1 = None
        power = None
        thermal_lvl = None
        temp_prot = None
        ocp_zone = None
        ocp_prot = None
        if self.device is not None:
            try:
                t = self.device.control.get_chip_temperature()
                ts0 = getattr(t, "ts0_temperature", None)
                ts1 = getattr(t, "ts1_temperature", None)
            except Exception:
                pass
            if self.power_started:
                power = self._read_power()
                if power is None:
                    self._power_fail_count += 1
                    if self._power_fail_count >= self._power_restart_threshold:
                        if self._start_power():
                            time.sleep(0.3)
                            power = self._read_power()
                else:
                    self._power_fail_count = 0
            if self._health_supported:
                try:
                    getter = (getattr(self.device.control,
                                      "_get_health_information", None)
                              or self.device.control.get_health_information)
                    h = getter()
                    thermal_lvl = int(h.current_temperature_throttling_level)
                    # OvercurrentAlertState is an IntEnum (GREEN=0, RED=1);
                    # coerce to int so the deque holds a plain Python int
                    # for CSV serialization.
                    ocp_zone = int(h.current_overcurrent_zone)
                    ocp_prot = 1 if h.overcurrent_throttling_active else 0
                    temp_prot = 1 if h.temperature_throttling_active else 0
                except Exception:
                    pass
        # Effective NN clock derived from the current throttle level:
        # if not throttling (lvl == -1 or None), the chip runs at the
        # firmware-requested baseline (typically 400 MHz on Hailo-8);
        # if throttling, look up the per-level frequency captured at
        # _probe_health_info().
        if thermal_lvl is None or thermal_lvl < 0:
            self._current_clock_mhz = self._requested_clock_mhz
        else:
            self._current_clock_mhz = self._throttle_level_freq_mhz.get(
                thermal_lvl, self._requested_clock_mhz)
        self.history_ts0.append(ts0)
        self.history_ts1.append(ts1)
        self.history_power.append(power)
        self._hist_thermal_throttle.append(thermal_lvl)
        self._hist_temp_prot.append(temp_prot)
        self._hist_ocp_zone.append(ocp_zone)
        self._hist_ocp_prot.append(ocp_prot)
        return ts0, ts1, power

    def _read_power(self):
        """One firmware-averaged power read. Returns None on failure/zero."""
        try:
            from hailo_platform import MeasurementBufferIndex
            data = self.device.control.get_power_measurement(
                buffer_index=(
                    MeasurementBufferIndex.MEASUREMENT_BUFFER_INDEX_0),
                should_clear=True)
            avg = getattr(data, "average_value", None)
            if avg is not None and avg > 0:
                return float(avg)
        except Exception:
            return None
        return None

    def reset_history(self):
        self.history_ts0.clear()
        self.history_ts1.clear()
        self.history_power.clear()
        self._hist_thermal_throttle.clear()
        self._hist_temp_prot.clear()
        self._hist_ocp_zone.clear()
        self._hist_ocp_prot.clear()

    def close(self):
        if self.power_started and self.device is not None:
            try:
                self.device.control.stop_power_measurement()
            except Exception:
                pass
            self.power_started = False
        self.device = None


class AxeleraProbe:
    """Owns a single Axelera Metis device and polls per-core temps.

    Power isn't exposed on Metis M.2 so the power history stays empty
    (TUI inline bar reads "--W" and the power trace is suppressed).
    Temperatures come from `triton_trace --slog --peek`, which prints
    the firmware's most recent collector log line; we enable the
    collector once at startup with --slog-level inf:collector and
    restore it to "err" on close to avoid leaking verbose logging.
    """

    HISTORY_MAX = 720  # default; overridable via __init__
    _TEMP_PAT = re.compile(r"core_temps=\[([\d,]+)\]")

    def __init__(self, bdf, sdk_device=None, verbose=False, history_max=None):
        self.bdf = bdf
        self.pcie = pcie_info(bdf) or {}
        self.identity = {"board_name": "Metis"}
        self.device = None
        self.error = None
        self._device_name = None  # axelera "metis-X:Y:Z" name
        self._collector_enabled = False
        self._triton_trace = _find_triton_trace()
        self._verbose = verbose
        self.history_max = history_max or self.HISTORY_MAX
        # Cached clock_profile (MHz) from
        # axr.Context().read_device_configuration() — captured once at
        # _open() since on Metis M.2 the clock is a boot-time config,
        # not an adaptive runtime value. Surfaced as `state_prefix`
        # so the panel header shows `CLOCK <MHz>` matching Hailo /
        # MemryX / DeepX layout.
        self._clock_mhz = None

        # Metis exposes 5 readings in triton_trace's `core_temps=[...]`
        # log line. Per Axelera's CONNECT documentation the order is:
        #   index 0: Sys-core   (module/system-level sensor)
        #   index 1: AI-core 0
        #   index 2: AI-core 1
        #   index 3: AI-core 2
        #   index 4: AI-core 3
        # We surface all five as separate metrics so the user can see
        # the module-level temperature alongside per-core thermals.
        self.history_t = [deque(maxlen=self.history_max) for _ in range(5)]
        self.history_power = deque(maxlen=self.history_max)
        # SYS uses magenta (matches PMD2's TOTAL "aggregate" color
        # convention); AI0..AI3 cycle through 4 distinct hues. POW
        # stays cyan even though it's always None on Metis M.2 — kept
        # so panels visually align with Hailo's POW slot.
        _LABELS = ("SYS", "AI0", "AI1", "AI2", "AI3")
        _COLORS = (CP_TRACE_TOTAL,  # SYS  magenta
                   CP_TRACE_TEMP,   # AI0  yellow
                   CP_OK,           # AI1  green
                   CP_TRACE_POWER,  # AI2  cyan (POW empty on Metis,
                                    #           so no clash)
                   CP_CRIT)         # AI3  red
        self.metrics = [
            Metric("POW", self.history_power, "W", CP_TRACE_POWER),
        ] + [
            Metric(_LABELS[i], self.history_t[i], "°C", _COLORS[i])
            for i in range(5)
        ]

        self._open(sdk_device)

    def _log(self, msg):
        """Print a probe-scoped diagnostic to stderr when --verbose."""
        if self._verbose:
            print(f"[AxeleraProbe {self.bdf}] {msg}", file=sys.stderr)

    def _open(self, sdk_device):
        if sdk_device is None:
            try:
                from axelera.runtime import objects as axr
            except ImportError as e:
                self.error = f"axelera.runtime not installed: {e}"
                self._log(self.error)
                return
            try:
                devs = list(axr.Context().list_devices())
            except Exception as e:
                msg = str(e).strip() or type(e).__name__
                self.error = msg[:60]
                self._log(f"list_devices() raised: {e}")
                return
            if not devs:
                self.error = "no devices enumerated by axelera.runtime"
                self._log(self.error)
                return
            sdk_device = devs[0]

        try:
            self.device = sdk_device
            self._device_name = getattr(sdk_device, "name", None)
            board_type = getattr(sdk_device, "board_type", None)
            arch = _axelera_board_arch(board_type)
            product = "Axelera Metis"
            if arch:
                product = f"{product} {arch} module"
            fw = getattr(sdk_device, "firmware_version", None)
            rev = getattr(sdk_device, "board_revision", None)
            desc_bits = []
            if fw:
                desc_bits.append(f"FW {fw}")
            if rev is not None:
                desc_bits.append(f"rev {rev}")
            self.identity = {
                "board_name": "Metis",
                "arch": arch,
                "product_name": product,
                "description": ", ".join(desc_bits),
                "serial": None,
                "part_number": None,
            }
            self._log(f"SDK device={self._device_name} arch={arch}")
            self._enable_collector()
            self._read_clock(sdk_device)
        except Exception as e:
            msg = str(e).strip() or type(e).__name__
            self.error = msg[:60]
            self.device = None
            self._log(f"_open raised: {e}")

    def _read_clock(self, sdk_device):
        """Cache the configured clock_profile (MHz) once at init.

        `axr.Context().read_device_configuration(dev)` returns 13
        static config keys (clocks, throttle thresholds, MVM
        utilization); `clock_profile` is the configured NN clock in
        MHz. On Metis M.2 this is boot-time-set, not adaptive, so a
        single read at init suffices. Best-effort: if axelera.runtime
        isn't importable here or the field is missing, we silently
        skip — the panel just doesn't get a CLOCK prefix.
        """
        try:
            from axelera.runtime import objects as axr
            ctx = axr.Context()
            cfg = ctx.read_device_configuration(sdk_device)
        except Exception as e:
            self._log(f"read_device_configuration() raised: {e}")
            return
        clock = cfg.get("clock_profile")
        if clock is None:
            self._log("clock_profile not in device configuration")
            return
        try:
            self._clock_mhz = float(clock)
        except (TypeError, ValueError):
            self._log(f"clock_profile not numeric: {clock!r}")
            return
        self._log(f"clock_profile = {self._clock_mhz:.0f} MHz")

    @property
    def state_prefix(self):
        """`(label, value_str)` for the panel header's CLOCK prefix.
        None if the clock_profile read failed at _open() time."""
        if self._clock_mhz is None:
            return None
        return ("CLOCK", f"{self._clock_mhz:.0f}MHz")

    def _enable_collector(self):
        """Switch triton_trace collector logging to "inf" so --peek has
        recent temperature samples to surface. Diagnostics are logged
        via _log() (visible only with --verbose)."""
        if not self._triton_trace:
            self._log("triton_trace not found (checked $PATH and "
                      "/opt/axelera/runtime-*/bin); temperatures will "
                      "read as --C")
            return
        if not self._device_name:
            self._log("no SDK device name; can't enable temp collector")
            return
        try:
            res = subprocess.run(
                [self._triton_trace, "--device", self._device_name,
                 "--slog-level", "inf:collector"],
                capture_output=True, timeout=5
            )
        except Exception as e:
            self._log(f"collector enable raised: {e}")
            return
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="replace").strip()
            self._log(f"collector enable failed (rc={res.returncode}): "
                      f"{err[:200]}")
            return
        self._collector_enabled = True
        self._log(f"temp collector enabled (bin={self._triton_trace}, "
                  f"device={self._device_name})")

    def poll(self):
        # _read_core_temps() returns a list of integers parsed from the
        # `core_temps=[a,b,c,d,e]` collector log line — typically 5 on
        # Metis. We pad with None / truncate so each of the 5 deques
        # gets exactly one sample per poll, matching Metric contract.
        temps = self._read_core_temps() if self._collector_enabled else None
        for i in range(5):
            v = float(temps[i]) if (temps and i < len(temps)) else None
            self.history_t[i].append(v)
        self.history_power.append(None)
        return temps, None

    _peek_fail_count = 0
    _peek_fail_logged = False

    def _read_core_temps(self):
        try:
            res = subprocess.run(
                [self._triton_trace, "--device", self._device_name,
                 "--slog", "--peek"],
                capture_output=True, timeout=3
            )
        except Exception as e:
            self._peek_fail_count += 1
            if self._peek_fail_count == 5 and not self._peek_fail_logged:
                self._peek_fail_logged = True
                self._log(f"--peek raised: {e}")
            return None
        if res.returncode != 0:
            self._peek_fail_count += 1
            if self._peek_fail_count == 5 and not self._peek_fail_logged:
                self._peek_fail_logged = True
                err = res.stderr.decode("utf-8", errors="replace").strip()
                self._log(f"--peek failed (rc={res.returncode}): "
                          f"{err[:200]}")
            return None
        text = res.stdout.decode("utf-8", errors="replace")
        temps = None
        for line in text.splitlines():
            m = self._TEMP_PAT.search(line)
            if m:
                temps = [int(x) for x in m.group(1).split(",")]
        if temps is None:
            self._peek_fail_count += 1
            if self._peek_fail_count == 5 and not self._peek_fail_logged:
                self._peek_fail_logged = True
                preview = text.strip().splitlines()
                preview = preview[-5:] if preview else ["<empty>"]
                self._log("--peek had no core_temps lines after 5 polls. "
                          "Last output:")
                for line in preview:
                    self._log(f"    {line}")
        else:
            self._peek_fail_count = 0
            self._peek_fail_logged = False
        return temps

    def reset_history(self):
        for d in self.history_t:
            d.clear()
        self.history_power.clear()

    def close(self):
        if (self._collector_enabled and self._triton_trace
                and self._device_name):
            try:
                subprocess.run(
                    [self._triton_trace, "--device", self._device_name,
                     "--slog-level", "err"],
                    capture_output=True, timeout=5
                )
            except Exception:
                pass
        self._collector_enabled = False
        self.device = None


class DeepXProbe:
    """Owns a single DeepX M1 NPU and polls per-NPU temperatures.

    Telemetry is only available through the `dxrt-cli` binary (DXRT
    3.2.0); the `dx_engine` Python package is for inference, not
    monitoring. We shell out to `dxrt-cli -s` and parse stdout.

    The M1 board has 3 NPU cores at fixed 1000 MHz / 750 mV. The
    firmware reports per-NPU voltage, clock, and temperature, but
    NOT power — so the POW history stays empty by design (same
    pattern as AxeleraProbe on Metis M.2). Voltage and clock are
    constants on this device, so we only graph temperature.
    """

    HISTORY_MAX = 720  # default; overridable via __init__
    NPU_COUNT = 3      # M1 has 3 NPU cores (NPU 0/1/2)

    # Match: " NPU 0: voltage 750 mV, clock 1000 MHz, temperature 29'C"
    # The trailing apostrophe-then-C is dxrt-cli's degree substitute.
    _NPU_LINE_PAT = re.compile(
        r"^\s*NPU\s+(\d+)\s*:.*?temperature\s+([\d.]+)\s*'?\s*C",
        re.IGNORECASE,
    )
    # Same line, capturing the per-NPU clock in MHz. Run as a separate
    # pattern (rather than extending _NPU_LINE_PAT to require both
    # clock and temperature in one match) so an SDK version that
    # changes the line order doesn't break temperature parsing.
    _CLOCK_LINE_PAT = re.compile(
        r"^\s*NPU\s+(\d+)\s*:.*?clock\s+([\d.]+)\s*MHz",
        re.IGNORECASE,
    )

    def __init__(self, bdf, verbose=False, history_max=None):
        self.bdf = bdf
        self.pcie = pcie_info(bdf) or {}
        self.identity = {"board_name": "DeepX M1"}
        self.device = None
        self.error = None
        self._verbose = verbose
        self._dxrt_cli = _find_dxrt_cli()
        self.history_max = history_max or self.HISTORY_MAX

        # Per-NPU temperature deques (T0..T(N-1)). Power is unsupported
        # on M1; we keep an empty deque so the POW slot lines up with
        # other probes' panels.
        self.history_t = [deque(maxlen=self.history_max)
                          for _ in range(self.NPU_COUNT)]
        self.history_power = deque(maxlen=self.history_max)
        # Live NN clock from `dxrt-cli -s`. All three M1 NPU cores
        # report the same value (board-level clock), so we cache the
        # most-recent first-NPU reading for the `state_prefix` CLOCK
        # widget. The skill notes voltage/clock are documented as
        # "static constants" on M1 — but they're still re-parsed each
        # poll, in case a future firmware ever varies them under load.
        self._clock_mhz = None

        # POW first to align visually with Hailo's POW slot (always
        # None on M1), then T0..T(N-1) in distinct colors.
        _T_COLORS = (CP_TRACE_TEMP,    # T0  yellow
                     CP_OK,            # T1  green
                     CP_TRACE_TOTAL)   # T2  magenta
        self.metrics = [
            Metric("POW", self.history_power, "W", CP_TRACE_POWER),
        ] + [
            Metric(f"T{i}", self.history_t[i], "°C", _T_COLORS[i])
            for i in range(self.NPU_COUNT)
        ]

        self._open()

    def _log(self, msg):
        """Print a probe-scoped diagnostic to stderr when --verbose."""
        if self._verbose:
            print(f"[DeepXProbe {self.bdf}] {msg}", file=sys.stderr)

    def _open(self):
        if not self._dxrt_cli:
            self.error = "dxrt-cli not found in $PATH or /usr/local/bin"
            self._log(self.error)
            return
        # Probe with -i once at startup to grab firmware/driver versions
        # and confirm the device responds.
        try:
            res = subprocess.run([self._dxrt_cli, "-i"],
                                 capture_output=True, timeout=5)
        except Exception as e:
            self.error = f"dxrt-cli -i raised: {str(e).strip()[:60]}"
            self._log(self.error)
            return
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="replace").strip()
            self.error = (f"dxrt-cli -i failed (rc={res.returncode}): "
                          f"{err[:60]}")
            self._log(self.error)
            return
        text = res.stdout.decode("utf-8", errors="replace")
        # Pull a few labelled fields out of the banner.
        fields = {}
        for line in text.splitlines():
            line = line.strip()
            for key, regex in (("fw_version",
                                r"\*\s*FW version\s*:\s*(\S+)"),
                               ("rt_driver",
                                r"\*\s*RT Driver version\s*:\s*(\S+)"),
                               ("pcie_driver",
                                r"\*\s*PCIe Driver version\s*:\s*(\S+)"),
                               ("memory",
                                r"\*\s*Memory\s*:\s*(.+)$"),
                               ("board",
                                r"\*\s*Board\s*:\s*(.+)$")):
                m = re.match(regex, line)
                if m:
                    fields[key] = m.group(1).strip()
        desc_bits = []
        if fields.get("fw_version"):
            desc_bits.append(f"FW {fields['fw_version']}")
        if fields.get("memory"):
            desc_bits.append(fields["memory"])
        self.identity = {
            "board_name": "DeepX M1",
            "arch": "M.2",
            "product_name": "DeepX DX_M1",
            "description": ", ".join(desc_bits),
            "serial": None,
            "part_number": None,
        }
        self.device = True  # truthy → not 'ERROR' in TUI
        self._log(f"opened (fw={fields.get('fw_version')}, "
                  f"rt={fields.get('rt_driver')}, "
                  f"pcie={fields.get('pcie_driver')})")

    def poll(self):
        temps = self._read_npu_temps() if self.device else None
        for i in range(self.NPU_COUNT):
            v = float(temps[i]) if (temps and i < len(temps)
                                    and temps[i] is not None) else None
            self.history_t[i].append(v)
        self.history_power.append(None)  # power not exposed on M1
        return temps, None

    _status_fail_count = 0
    _status_fail_logged = False

    def _read_npu_temps(self):
        """Run `dxrt-cli -s` and parse per-NPU temperature lines.

        Returns a list of length NPU_COUNT, indexed by NPU id; entries
        are float °C or None where the parse missed. Returns None if
        the CLI call fails entirely.
        """
        try:
            res = subprocess.run([self._dxrt_cli, "-s"],
                                 capture_output=True, timeout=5)
        except Exception as e:
            self._status_fail_count += 1
            if (self._status_fail_count == 5
                    and not self._status_fail_logged):
                self._status_fail_logged = True
                self._log(f"dxrt-cli -s raised: {e}")
            return None
        if res.returncode != 0:
            self._status_fail_count += 1
            if (self._status_fail_count == 5
                    and not self._status_fail_logged):
                self._status_fail_logged = True
                err = res.stderr.decode("utf-8", errors="replace").strip()
                self._log(f"dxrt-cli -s failed (rc={res.returncode}): "
                          f"{err[:200]}")
            return None
        text = res.stdout.decode("utf-8", errors="replace")
        by_idx = {}
        clocks = []
        for line in text.splitlines():
            m = self._NPU_LINE_PAT.match(line)
            if m:
                by_idx[int(m.group(1))] = float(m.group(2))
            mc = self._CLOCK_LINE_PAT.match(line)
            if mc:
                try:
                    clocks.append(float(mc.group(2)))
                except (TypeError, ValueError):
                    pass
        if clocks:
            # All cores report the same clock — first reading is fine.
            self._clock_mhz = clocks[0]
        if not by_idx:
            self._status_fail_count += 1
            if (self._status_fail_count == 5
                    and not self._status_fail_logged):
                self._status_fail_logged = True
                preview = text.strip().splitlines()[-5:] or ["<empty>"]
                self._log("dxrt-cli -s had no NPU lines after 5 polls. "
                          "Last output:")
                for line in preview:
                    self._log(f"    {line}")
            return None
        # Reset on success
        self._status_fail_count = 0
        self._status_fail_logged = False
        return [by_idx.get(i) for i in range(self.NPU_COUNT)]

    @property
    def state_prefix(self):
        """`(label, value_str)` for the panel header's CLOCK prefix.
        None until the first successful `dxrt-cli -s` parse populates
        `self._clock_mhz`."""
        if self._clock_mhz is None:
            return None
        return ("CLOCK", f"{self._clock_mhz:.0f}MHz")

    def reset_history(self):
        for d in self.history_t:
            d.clear()
        self.history_power.clear()

    def close(self):
        # dxrt-cli is fire-and-forget — nothing to release.
        self.device = None


class MemryXProbe:
    """Owns a single MemryX MX3 NPU and polls power + per-MPU temperatures.

    Telemetry comes from two sources (a hybrid pattern unique among
    this project's probes):

    - **memryx.mxa SDK module** (when installed): power (mW → W), clock
      (MHz), voltage (mV), thermal state, power alert. Always-on
      readings — no DFP, no active inference required. Source for the
      POW + CLK metrics on the TUI panel and for the VOLT /
      THERMAL_STATE / POWER_ALERT extras in `last_snapshot` / `--once`.
      Capability is gated per-device via memryx.mxa.get_power()
      raising — older modules / SDK revisions may not support it.

    - **hwmon (kernel)**: per-MPU temperatures. The
      `memx_pcie_ai_chip` driver registers a standard Linux hwmon
      node at /sys/class/hwmon/hwmonN/ with name="memx0", exposing
      temp1_input..temp16_input. On the 4-chip MX3-2280-M-4 SKU
      temp1..temp4 populate (one per MPU, matching the MPU 0 → MPU 3
      dataflow pipeline); on the 2-chip SKU only temp1..temp2 populate.
      We probe at __init__ to autodetect how many MPU sensors are live.

    The hwmon per-MPU values are richer than the SDK's single scalar
    `get_temperature()` (which reports a board-level average — about
    10 °C below the actual per-MPU die readings), so we always prefer
    hwmon for the T0..T<n> metrics even when the SDK is available.

    When the SDK isn't installed (or `can_get_power_consumption` is
    False on this device), the probe degrades to "hwmon only" — POW
    stays empty by design, matching the original Axelera/DeepX-style
    no-power pattern.
    """

    HISTORY_MAX = 720      # default; overridable via __init__
    MAX_MPUS = 16          # driver reserves temp1..temp16

    def __init__(self, bdf, verbose=False, history_max=None,
                 hwmon_path=None, device_id=0):
        self.bdf = bdf
        self.pcie = pcie_info(bdf) or {}
        self.identity = {"board_name": "MemryX MX3"}
        self.device = None
        self.error = None
        self._verbose = verbose
        self.history_max = history_max or self.HISTORY_MAX
        self.hwmon_path = hwmon_path   # optional explicit override
        self._populated_idxs = []

        # SDK state. Populated by _open_sdk(); used by poll().
        # device_id: the SDK's per-host MX3 module index (0 for the
        # first/only module). TODO multi-module: derive from `bdf`
        # by matching `device_info_t` entries to the PCI BDF when
        # multiple MX3s are present on one host.
        self.device_id = device_id
        self._mxa = None             # memryx.mxa module reference
        self._sdk_version = None     # memryx.__version__
        self._has_power = False      # True iff get_power() returned OK

        # Snapshot of SDK extras (clock, voltage, thermal state, power
        # alert) captured on each poll for --once display; not graphed.
        # Clock frequency is a state indicator (which boost mode is
        # active), not a metric to graph over time — it lives here
        # rather than as a Metric.
        self.last_snapshot = None

        self.history_power = deque(maxlen=self.history_max)
        self.history_t = []   # populated in _open() after sensor scan

        # state_flags: list of (label, deque) pairs. The TUI renders
        # them inline as `<LABEL> [state]` badges. Same-prefix labels
        # (e.g. THERMAL_0..THERMAL_3) are visually grouped by the TUI
        # into one bracket. The CSV writer emits each as a per-flag
        # column. Distinct from `metrics` because flags are enum state
        # indicators (not graphed as time-series, not rendered as
        # inline bars).
        #
        # Per-chip thermal throttle state comes from the vendor sysfs
        # node `/sys/memx<N>/temperature` (parsed in _read_throttle()).
        # The SDK's `get_thermal_state(device_id)` only returns one
        # module-wide value (no chip_id arg) — effectively the OR of
        # these per-chip latches — so sysfs is the only source for
        # per-chip granularity and we use it unconditionally (no SDK
        # dependency for thermal). Power alert remains SDK-only.
        self._hist_thermal_states = []  # per-chip; populated after _open()
        self._hist_power_alert    = deque(maxlen=self.history_max)
        self.state_flags = []   # populated below

        self._open()          # hwmon discovery + identity + temps
        self._open_sdk()      # memryx import + power capability check

        # Probe the per-chip throttle-state count from the vendor
        # sysfs file. Falls back to len(self._populated_idxs) (i.e.
        # one slot per populated hwmon temp) when the sysfs file is
        # unreadable; that mirrors the chip count anyway.
        self._n_throttle_chips = self._probe_throttle_chip_count()

        # Build the metrics list AFTER both _open() and _open_sdk():
        # we need _populated_idxs (T-count) and _has_power (POW
        # presence). Layout principle: power slot first to align
        # with Hailo's POW slot; per-MPU temps after.
        #
        # Color order is chosen so adjacent metrics on the inline-bars
        # row read distinctly: POW=cyan, then T's cycle through
        # magenta/yellow/green/red.
        _T_COLORS = (CP_TRACE_TOTAL,   # T0 magenta — pipeline input
                     CP_TRACE_TEMP,    # T1 yellow
                     CP_OK,            # T2 green
                     CP_CRIT,          # T3 red
                     CP_ACCENT)        # T4+ cyan, if future denser SKU

        self.history_t = [deque(maxlen=self.history_max)
                          for _ in self._populated_idxs]
        self.metrics = []
        # POW slot is always present for panel alignment — but its
        # values are only non-None when SDK power capability exists.
        self.metrics.append(
            Metric("POW", self.history_power, "W", CP_TRACE_POWER))
        for i in range(len(self._populated_idxs)):
            self.metrics.append(
                Metric(f"T{i}",
                       self.history_t[i],
                       "°C",
                       _T_COLORS[i % len(_T_COLORS)]))
        # state_flags assembly:
        #   - THERMAL_<i> (one per chip) from sysfs — always present
        #     when the sysfs file is readable.
        #   - POWER (module-level) from SDK — only when get_power()
        #     capability is confirmed.
        # The TUI groups THERMAL_0..N-1 into a single THERMAL widget;
        # CSV emits one column per per-chip flag plus a POWER column.
        self._hist_thermal_states = [deque(maxlen=self.history_max)
                                     for _ in range(self._n_throttle_chips)]
        for i in range(self._n_throttle_chips):
            self.state_flags.append(
                (f"THERMAL_{i}", self._hist_thermal_states[i]))
        if self._has_power:
            self.state_flags.append(("POWER", self._hist_power_alert))

    @property
    def state_prefix(self):
        """Tuple `(label, value_str)` rendered as the leading
        `LABEL value` pair on the status row before the state-flag
        badges — for MemryX, the current clock frequency.

        Returns `None` when no SDK reading is available; the TUI then
        omits the prefix entirely and the row starts with the first
        flag badge. Clock isn't graphed (it's a power-mode indicator,
        not a load-varying metric), so it lives here rather than as a
        Metric — matches the user's "CLK is not a metric" feedback.
        """
        snap = self.last_snapshot
        if not snap:
            return None
        freq = snap.get("frequency_mhz")
        if freq is None:
            return None
        return ("CLOCK", f"{freq:.0f}MHz")

    def _log(self, msg):
        """Print a probe-scoped diagnostic to stderr when --verbose."""
        if self._verbose:
            print(f"[MemryXProbe {self.bdf}] {msg}", file=sys.stderr)

    def _read_throttle(self):
        """Read per-chip ThermalThrottlingState from /sys/memx<N>/temperature.

        Returns a list of int throttle states (one per chip, in CHIP()
        index order), or None on any read/parse error. The sysfs file
        format is human-readable, one line per chip; we extract the
        ThermalThrottlingState field via MEMRYX_SYSFS_RE.
        """
        path = MEMRYX_SYSFS_FMT.format(self.device_id)
        try:
            with open(path) as f:
                raw = f.read()
        except OSError:
            return None
        # Order by CHIP(N) index so we don't depend on file line order.
        per_chip = {}
        for m in MEMRYX_SYSFS_RE.finditer(raw):
            per_chip[int(m.group(1))] = int(m.group(2))
        if not per_chip:
            return None
        return [per_chip[i] for i in sorted(per_chip)]

    def _probe_throttle_chip_count(self):
        """Determine how many CHIP() entries the sysfs file exposes,
        for sizing per-chip state_flags up front. Falls back to the
        hwmon-populated temp-sensor count when sysfs is unreadable —
        those should match on real hardware."""
        states = self._read_throttle()
        if states is not None:
            return len(states)
        fallback = len(self._populated_idxs)
        self._log(f"sysfs throttle file unreadable, falling back to "
                  f"hwmon count = {fallback}")
        return fallback

    def _find_hwmon_for_bdf(self):
        """Scan /sys/class/hwmon for an entry matching self.bdf.

        Returns the hwmon directory path, or None if not found.
        """
        for d in sorted(glob.glob(os.path.join(SYSFS_HWMON, "hwmon*"))):
            try:
                with open(os.path.join(d, "name")) as f:
                    if f.read().strip() != MEMRYX_HWMON_NAME:
                        continue
            except OSError:
                continue
            dev_link = os.path.join(d, "device")
            if not os.path.islink(dev_link):
                continue
            try:
                target = os.readlink(dev_link)
            except OSError:
                continue
            if os.path.basename(target) == self.bdf:
                return d
        return None

    def _open(self):
        # Find the hwmon node for this BDF (unless caller pre-supplied).
        if self.hwmon_path is None:
            self.hwmon_path = self._find_hwmon_for_bdf()
        if self.hwmon_path is None:
            self.error = (f"hwmon name='{MEMRYX_HWMON_NAME}' not found "
                          f"for BDF {self.bdf} — is memx_pcie_ai_chip "
                          f"loaded?")
            self._log(self.error)
            return

        # Probe which temp slots are populated. The driver reserves
        # temp1..temp16 but only the populated slots return values.
        for i in range(1, self.MAX_MPUS + 1):
            fn = os.path.join(self.hwmon_path, f"temp{i}_input")
            try:
                with open(fn) as f:
                    raw = f.read().strip()
                if raw:
                    self._populated_idxs.append(i)
            except OSError:
                continue
        if not self._populated_idxs:
            self.error = "no populated temp sensors in hwmon"
            self._log(self.error)
            return

        n = len(self._populated_idxs)
        # arch is intentionally period-free — the TUI/snapshot
        # formatter splits on '.' to unwrap enum qualnames like
        # 'BoardType.METIS', which would eat "M.2" too. Use "M2"
        # instead. Chip-count info lives in product_name.
        self.identity = {
            "board_name": "MemryX MX3",
            "arch": f"M2 ({n}-chip)",
            "product_name": f"MemryX MX3-2280-M-{n}",
            "description": f"hwmon={os.path.basename(self.hwmon_path)}",
            "serial": None,
            "part_number": None,
        }
        self.device = True   # truthy → not 'ERROR' in TUI
        self._log(f"opened: hwmon={self.hwmon_path}, "
                  f"populated_temps={self._populated_idxs}")

    def _open_sdk(self):
        """Detect memryx SDK + per-device power capability.

        Three outcomes:
        - SDK absent (memryx not installed): _has_power stays False,
          POW metric still listed but values stay None.
        - SDK present but get_power() raises / returns nonsense on
          this device_id: same fallback.
        - SDK present + get_power() returns a number: _has_power=True,
          metrics include POW + CLK with real values from poll().
        """
        try:
            import memryx as _memryx
            self._mxa = _memryx.mxa
            self._sdk_version = getattr(_memryx, "__version__", "?")
        except Exception as e:
            self._log(f"memryx SDK not importable; POW disabled: "
                      f"{type(e).__name__}: {e}")
            return
        # Try a power read directly — the simplest capability check.
        # The MxAccl.can_get_power_consumption() method works too but
        # requires constructing an MxAccl with a DFP, which is heavy
        # and unnecessary here.
        try:
            p = self._mxa.get_power(self.device_id)
            if isinstance(p, (int, float)) and p >= 0:
                self._has_power = True
                self._log(f"SDK {self._sdk_version} loaded, power "
                          f"capability OK (initial {p} mW)")
            else:
                self._log(f"SDK power query returned non-numeric: "
                          f"{p!r}; POW disabled")
        except Exception as e:
            self._log(f"SDK power query raised; POW disabled: "
                      f"{type(e).__name__}: {e}")
        if self._has_power:
            # Augment identity with SDK version + capability flag
            desc = self.identity.get("description", "")
            self.identity["description"] = (
                f"{desc}, SDK={self._sdk_version}, power=supported")

    def poll(self):
        if not self.device:
            for d in self.history_t:
                d.append(None)
            self.history_power.append(None)
            for d in self._hist_thermal_states:
                d.append(None)
            self._hist_power_alert.append(None)
            return None, None
        temps = []
        for idx, deq in zip(self._populated_idxs, self.history_t):
            fn = os.path.join(self.hwmon_path, f"temp{idx}_input")
            try:
                with open(fn) as f:
                    raw = f.read().strip()
                v = float(raw) / 1000.0 if raw else None
            except (OSError, ValueError):
                v = None
            temps.append(v)
            deq.append(v)
        # Per-chip throttle state from /sys/memx<N>/temperature.
        # Independent of SDK — works as soon as the driver is loaded.
        throttles = self._read_throttle()
        for i, d in enumerate(self._hist_thermal_states):
            d.append(throttles[i] if (throttles is not None
                                      and i < len(throttles)) else None)
        # SDK readings — power (mW→W) to history, clock/volt to
        # last_snapshot, power_alert enum to state_flags history (for
        # CSV) and last_snapshot (for --once).
        if self._has_power and self._mxa is not None:
            p_w = clk = volt = pwr_alert = None
            try:
                p_w = self._mxa.get_power(self.device_id) / 1000.0
            except Exception:
                pass
            try:
                clk = float(self._mxa.get_frequency(self.device_id, 0))
            except Exception:
                pass
            try:
                volt = self._mxa.get_voltage(self.device_id)
            except Exception:
                pass
            try:
                pwr_alert = self._mxa.get_poweralert(self.device_id)
            except Exception:
                pass
            self.history_power.append(p_w)
            self._hist_power_alert.append(pwr_alert)
            self.last_snapshot = {
                "voltage_mv": volt,
                "thermal_states": list(throttles) if throttles else None,
                "power_alert": pwr_alert,
                "frequency_mhz": clk,
            }
        else:
            self.history_power.append(None)
            self._hist_power_alert.append(None)
            # Still record per-chip throttle for --once even without SDK
            self.last_snapshot = {
                "thermal_states": list(throttles) if throttles else None,
            }
        return temps, (self.history_power[-1]
                       if self.history_power else None)

    def reset_history(self):
        for d in self.history_t:
            d.clear()
        self.history_power.clear()
        for d in self._hist_thermal_states:
            d.clear()
        self._hist_power_alert.clear()

    def close(self):
        # Pure sysfs + SDK function calls — nothing to release. SDK
        # is module-level; we don't own any handles.
        self.device = None


class PMD2Probe:
    """ElmorLabs PMD2 power-measurement device probe.

    USB CDC (STM32 VID:PID 0483:5740) at 115200 baud. The TUI surfaces
    three values: PCIE1, PCIE2, and PCIE3 rail power — the three slot
    rails an AI accelerator AIC typically draws from. They share the
    POW (watts) axis so a single graph window plots all three traces
    in distinct colors.

    The PMD2 also reports an STM32 internal Tchip but that's the
    measurement device's own MCU temperature — not the system or any
    accelerator — so it's deliberately suppressed (no temp metric).
    The other 7 rails (ATX12V, ATX5V, ATX5VSB, ATX3.3V, HPWR1, EPS1/2) plus the
    EPS/PCIe/MB/Total aggregates are still polled, parsed, and dumped
    by --once snapshots, but not graphed.

    Protocol cribbed from https://github.com/ElmorLabs/PMD2-Python:
      CMD_READ_VENDOR_DATA  = 0x01  -> 3 bytes (vid, pid, fw_version)
      CMD_READ_SENSOR_VALUES= 0x04  -> SensorStruct (122 bytes packed)

    SensorStruct layout (little-endian, packed):
      uint16  Vdd
      int16   Tchip                                         (suppressed)
      10 x { int16 Voltage_mV; int32 Current_mA; int32 Power_mW }
      uint16  EpsPower, PciePower, MbPower, TotalPower      (aggregate W)
      uint8 x 10  Ocp                                       (status)
    """

    HISTORY_MAX = 720  # default; overridable via __init__

    # struct format: <Hh + (hii)*10 + HHHH + 10B  = 122 bytes
    _SENSOR_FMT = "<Hh" + "hii" * 10 + "HHHH" + "10B"
    _SENSOR_SIZE = struct.calcsize(_SENSOR_FMT)

    RAIL_NAMES = ["ATX12V", "ATX5V", "ATX5VSB", "ATX3.3V", "HPWR1",
                  "EPS1", "EPS2", "PCIE1", "PCIE2", "PCIE3"]
    PCIE_RAIL_INDICES = [7, 8, 9]
    PCIE_RAIL_COLORS = [CP_TRACE_POWER, CP_OK, CP_TRACE_TEMP]

    # TOTAL_MAX is per-metric (TOTAL goes to ~100 W on a workstation;
    # PCIE rails sit in the 1-10 W range, controlled by TUI's
    # --power-max). PMD2 TOTAL keeps its own scale via max_val on
    # the metric, so this constant is used.
    TOTAL_MAX = 100.0

    def __init__(self, port_path, verbose=False, history_max=None):
        self.bdf = port_path
        self.pcie = {}  # USB device — no PCIe info to render
        self.identity = {"board_name": "PMD2"}
        self.device = None  # serial.Serial when opened
        self.error = None
        self._verbose = verbose
        self.history_max = history_max or self.HISTORY_MAX

        self.history_temp = deque(maxlen=self.history_max)
        self.history_power = deque(maxlen=self.history_max)

        self.history_total = deque(maxlen=self.history_max)
        self.history_pcie1 = deque(maxlen=self.history_max)
        self.history_pcie2 = deque(maxlen=self.history_max)
        self.history_pcie3 = deque(maxlen=self.history_max)
        self._rail_histories = [
            self.history_pcie1, self.history_pcie2, self.history_pcie3,
        ]
        self.metrics = [
            Metric("PCIE1", self.history_pcie1, "W", CP_TRACE_POWER),
            Metric("PCIE2", self.history_pcie2, "W", CP_OK),
            Metric("PCIE3", self.history_pcie3, "W", CP_TRACE_TEMP),
            Metric("TOTAL", self.history_total, "W", CP_TRACE_TOTAL,
                   max_val=self.TOTAL_MAX),
        ]

        self.last_snapshot = None

        self._open(port_path)

    def _log(self, msg):
        if self._verbose:
            print(f"[PMD2Probe {self.bdf}] {msg}", file=sys.stderr)

    def _open(self, port_path):
        try:
            import serial
        except ImportError as e:
            self.error = f"pyserial not installed: {e}"
            self._log(self.error)
            return
        try:
            ser = serial.Serial(port_path, 115200, timeout=1)
        except Exception as e:
            self.error = f"open {port_path} failed: {e}"
            self._log(self.error)
            return
        try:
            ser.timeout = 0.2
            ser.read(64)
        except Exception:
            pass
        ser.timeout = 1.0

        fw = None
        try:
            ser.write(bytes([0x01]))
            data = ser.read(3)
            if len(data) == 3:
                _, _, fw_byte = struct.unpack("<BBB", data)
                fw = fw_byte
        except Exception as e:
            self._log(f"vendor read failed: {e}")

        self.device = ser
        self.identity = {
            "board_name": "PMD2",
            "arch": "USB",
            "product_name": "ElmorLabs PMD2",
            "description": f"FW v{fw}" if fw is not None else "",
            "serial": None,
            "part_number": None,
        }
        self._log(f"opened (fw={fw})")

    def poll(self):
        rail_powers = [None, None, None]
        total_w = None
        if self.device is not None:
            try:
                self.device.reset_input_buffer()
                self.device.write(bytes([0x04]))
                raw = self.device.read(self._SENSOR_SIZE)
            except Exception as e:
                self._log(f"sensor read raised: {e}")
                raw = b""
            if len(raw) == self._SENSOR_SIZE:
                fields = struct.unpack(self._SENSOR_FMT, raw)
                for i, rail_idx in enumerate(self.PCIE_RAIL_INDICES):
                    base = 2 + rail_idx * 3
                    rail_powers[i] = fields[base + 2] / 1000.0
                total_w = float(fields[35])
                self.last_snapshot = self._unpack_snapshot(fields)
            else:
                self._log(f"sensor read short ({len(raw)}/"
                          f"{self._SENSOR_SIZE} bytes)")
        self.history_total.append(total_w)
        for hist, p in zip(self._rail_histories, rail_powers):
            hist.append(p)
        return total_w, rail_powers

    def _unpack_snapshot(self, fields):
        """Decode the SensorStruct tuple into a dict (for do_snapshot)."""
        rails = []
        for i in range(10):
            base = 2 + i * 3
            rails.append({
                "name": self.RAIL_NAMES[i],
                "voltage_v": fields[base] / 1000.0,
                "current_a": fields[base + 1] / 1000.0,
                "power_w":   fields[base + 2] / 1000.0,
            })
        atx24_w = sum(r["power_w"] for r in rails[:4])
        return {
            "vdd_mv":      fields[0],
            "rails":       rails,
            "eps_w":       fields[32],
            "pcie_w":      fields[33],
            "mb_w":        fields[34],
            "atx24_w":     atx24_w,
            "total_w":     fields[35],
            "ocp":         list(fields[36:46]),
        }

    def reset_history(self):
        self.history_total.clear()
        for hist in self._rail_histories:
            hist.clear()

    def close(self):
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass
        self.device = None


class INA228Probe:
    """One or more Adafruit INA228 sensors on a single FT232H bus.

    The Adafruit FT232H breakout is a USB→MPSSE I2C bridge; up to
    four INA228 power monitors can share that bus, with their I2C
    addresses set by the A0/A1 strap pins of the breakout. The
    canonical address set is {0x40, 0x41, 0x44, 0x45} — `0x40` is
    the strap-default ("current sensor" in the user's setup), the
    other three are the alternate strap combinations.

    On _open() we:
      1. Set BLINKA_FT232H=1 and import `board` + adafruit_ina228
      2. Build the shared I2C bus with `board.I2C()`
      3. Scan the bus (i2c.scan()) for the canonical INA228 addresses
      4. Instantiate INA228(i2c, addr=N) for each that responds,
         calibrate with the shared shunt / max_current, and configure
         averaging (COUNT_64 + 1052 µs conversions — the
         "smooth noisy readings" config from query_adafruit_sensor.py)

    One panel is rendered — one POW (W) trace per detected sensor,
    color-coded so the eye can tell them apart:

      0x40 → cyan   (CP_TRACE_POWER)   — "current sensor", strap default
      0x41 → green  (CP_OK)
      0x44 → yellow (CP_TRACE_TEMP)
      0x45 → magenta(CP_TRACE_TOTAL)

    Per-sensor bus voltage and current are still tracked in history
    and dumped by `--once`; only POW is graphed (3 metrics × 4 sensors
    = 12 traces would overcrowd a single panel).

    Single-instance — Blinka's BLINKA_FT232H mode picks the first
    FT232H on the host's USB bus, so multiple FT232Hs aren't
    addressed by this probe.

    Calibration knobs (all CLI-overridable, shared across sensors):
      --ina228-shunt OHM         shunt resistance (default 0.015 Ω)
      --ina228-max-current A     full-scale current (default 5 A —
                                 sized for Hailo-8 / Metis M.2;
                                 raise to 10 A for higher-TDP cards)
      --ina228-addresses LIST    comma-separated addresses to try
                                 (default: 0x40,0x41,0x44,0x45)
    """

    HISTORY_MAX = 720  # default; overridable via __init__

    _STANDARD_RAILS = [
        (1.05, "1.05V"), (1.2, "1.2V"), (1.5, "1.5V"), (1.8, "1.8V"),
        (3.3, "3.3V"), (5.0, "5V"), (12.0, "12V"),
    ]
    _RAIL_TOLERANCE = 0.10  # ±10% of nominal counts as a match
    _RAIL_CLASSIFY_AFTER = 3

    @classmethod
    def _classify_rail(cls, v):
        """Map a measured voltage (V) to a label string."""
        if v is None or v <= 0:
            return None
        for nominal, label in cls._STANDARD_RAILS:
            if abs(v - nominal) / nominal < cls._RAIL_TOLERANCE:
                return label
        return f"{v:.1f}V"

    KNOWN_ADDRESSES = (0x40, 0x41, 0x44, 0x45)

    ADDR_TO_INDEX = {a: i + 1 for i, a in enumerate(KNOWN_ADDRESSES)}

    PER_ADDR_COLOR = {
        0x40: CP_TRACE_POWER,   # cyan   — strap default
        0x41: CP_OK,            # green
        0x44: CP_TRACE_TEMP,    # yellow
        0x45: CP_TRACE_TOTAL,   # magenta
    }

    def __init__(self, bdf="adafruit-ft232h", addresses=None,
                 shunt_res=0.015, max_current=5.0,
                 verbose=False, history_max=None):
        self.bdf = bdf
        self.pcie = {}                      # not a PCI device
        self.identity = {"board_name": "Adafruit"}
        self.device = None                  # truthy after first sensor opens
        self.error = None
        self._verbose = verbose
        self.history_max = history_max or self.HISTORY_MAX

        self._wanted_addresses = (tuple(addresses) if addresses
                                  else self.KNOWN_ADDRESSES)
        self._shunt_res = shunt_res
        self._max_current = max_current

        self._sensors = []                  # list[(addr, INA228 instance)]
        self._addresses = []                # list[addr], same order as metrics
        self._hist_pow = {}                 # addr -> deque[W]
        self._hist_v   = {}                 # addr -> deque[V]
        self._hist_i   = {}                 # addr -> deque[A]
        self._labels_finalized = {}         # addr -> bool

        self.history_temp = deque(maxlen=self.history_max)
        self.history_power = deque(maxlen=self.history_max)

        self.metrics = []

        self.last_snapshot = None

        self._open()

    def _log(self, msg):
        if self._verbose:
            print(f"[INA228Probe {self.bdf}] {msg}", file=sys.stderr)

    def _open(self):
        os.environ.setdefault("BLINKA_FT232H", "1")
        try:
            with _silence_fd_output():
                import board
                from adafruit_ina228 import (
                    INA228, AveragingCount, ConversionTime,
                )
        except ImportError as e:
            self.error = (f"adafruit-blinka / adafruit-circuitpython-"
                          f"ina228 not installed: {e}")
            self._log(self.error)
            return
        except Exception as e:
            self.error = f"FT232H init failed: {str(e).strip()[:60]}"
            self._log(self.error)
            return

        try:
            with _silence_fd_output():
                i2c = board.I2C()             # SCL=D0, SDA=D1+D2 on FT232H
        except Exception as e:
            self.error = f"FT232H I2C bus init failed: {str(e).strip()[:60]}"
            self._log(self.error)
            return

        try:
            with _silence_fd_output():
                while not i2c.try_lock():
                    pass
                try:
                    seen = set(i2c.scan())
                finally:
                    i2c.unlock()
        except Exception as e:
            self.error = f"i2c scan failed: {str(e).strip()[:60]}"
            self._log(self.error)
            return

        present = [a for a in self._wanted_addresses if a in seen]
        if not present:
            wanted_s = ", ".join(f"0x{a:02X}" for a in self._wanted_addresses)
            seen_s = (", ".join(f"0x{a:02X}" for a in sorted(seen))
                      or "none")
            self.error = (f"no INA228 found at {wanted_s} "
                          f"(bus has: {seen_s})")
            self._log(self.error)
            return
        self._log("i2c scan found INA228 at: "
                  + ", ".join(f"0x{a:02X}" for a in present))

        ok_addrs = []
        for addr in present:
            try:
                with _silence_fd_output():
                    ina = INA228(i2c, address=addr)
                    ina.set_calibration(
                        shunt_res=self._shunt_res,
                        max_current=self._max_current,
                    )
                    ina.averaging_count = AveragingCount.COUNT_64
                    ina.bus_voltage_conv_time = ConversionTime.TIME_1052_US
                    ina.shunt_voltage_conv_time = ConversionTime.TIME_1052_US
                self._sensors.append((addr, ina))
                self._hist_pow[addr] = deque(maxlen=self.history_max)
                self._hist_v[addr]   = deque(maxlen=self.history_max)
                self._hist_i[addr]   = deque(maxlen=self.history_max)
                ok_addrs.append(addr)
            except Exception as e:
                self._log(f"INA228@0x{addr:02X} setup failed: {e}")

        if not self._sensors:
            self.error = "all detected INA228s failed calibration"
            self._log(self.error)
            return

        self._addresses = list(ok_addrs)
        # Note: per-metric max_val intentionally left as None so the
        # TUI's --power-max (passed to TUI.POWER_MAX) governs the axis
        # cap. Setting max_val=self.POWER_MAX here would have hardcoded
        # 10 W and made --power-max unreachable for INA228 readings.
        self.metrics = [
            Metric(f"P{self.ADDR_TO_INDEX.get(addr, '?')}",
                   self._hist_pow[addr], "W",
                   self.PER_ADDR_COLOR.get(addr, CP_TRACE_POWER))
            for addr in ok_addrs
        ]

        self.device = self._sensors[0][1]  # truthy → not 'ERROR'
        self.identity = {
            "board_name": "Adafruit",
            "arch": "I2C",
            "product_name": (f"INA228 ×{len(self._sensors)} via FT232H"
                             if len(self._sensors) > 1
                             else "INA228 via FT232H"),
            "description": (
                f"{self._shunt_res * 1000:g}mΩ shunt, "
                f"max {self._max_current:g}A; addrs "
                + ", ".join(f"0x{a:02X}" for a in ok_addrs)),
            "serial": None,
            "part_number": None,
        }
        self._log(f"opened {len(self._sensors)} sensor(s) "
                  f"(shunt={self._shunt_res}Ω, "
                  f"max_current={self._max_current}A)")

    def poll(self):
        """Read every detected INA228; populate per-sensor histories
        and the legacy `history_power` (sum across sensors)."""
        total_w = 0.0
        any_ok = False
        snap = []
        for (addr, ina) in self._sensors:
            v = current = power = None
            try:
                v = float(ina.bus_voltage)
                current = float(ina.current)
                power = float(ina.power)
            except Exception as e:
                self._log(f"INA228@0x{addr:02X} read raised: {e}")
            self._hist_v[addr].append(v)
            self._hist_i[addr].append(current)
            self._hist_pow[addr].append(power)
            if power is not None:
                total_w += power
                any_ok = True
            snap.append({"addr": addr, "voltage_v": v,
                         "current_a": current, "power_w": power})
        self.history_power.append(total_w if any_ok else None)
        self.last_snapshot = {"sensors": snap}
        self._maybe_finalize_labels()
        return None

    def _maybe_finalize_labels(self):
        for i, addr in enumerate(self._addresses):
            if self._labels_finalized.get(addr):
                continue
            valid = [s for s in self._hist_v[addr] if s is not None]
            if len(valid) < self._RAIL_CLASSIFY_AFTER:
                continue
            avg_v = sum(valid[-self._RAIL_CLASSIFY_AFTER:]) \
                / self._RAIL_CLASSIFY_AFTER
            rail = self._classify_rail(avg_v)
            if rail is None:
                continue
            n = self.ADDR_TO_INDEX.get(addr, "?")
            old = self.metrics[i]
            self.metrics[i] = old._replace(label=f"P{n}({rail})")
            self._labels_finalized[addr] = True
            self._log(f"INA228@0x{addr:02X} classified as {rail} rail "
                      f"(avg {avg_v:.3f}V over "
                      f"{self._RAIL_CLASSIFY_AFTER} samples)")

    @property
    def metrics_settled(self):
        """All sensor labels have been finalized — every detected
        INA228 has seen `_RAIL_CLASSIFY_AFTER` valid voltage reads
        and had its `P{n}({rail})` label stamped in. The CSV writer
        in TUI uses this to defer the header until the column names
        reflect the post-classification labels rather than the
        initial `P{n}` placeholders.
        """
        if not self._addresses:
            return True
        return all(self._labels_finalized.get(a)
                   for a in self._addresses)

    def reset_history(self):
        for d in self._hist_pow.values():
            d.clear()
        for d in self._hist_v.values():
            d.clear()
        for d in self._hist_i.values():
            d.clear()
        self.history_power.clear()

    def close(self):
        self._sensors = []
        self.device = None


# ---------------------------------------------------------------------------
# Curses rendering
# ---------------------------------------------------------------------------


def _init_colors():
    """Define the color palette. Must be called after initscr()."""
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(CP_TITLE, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(CP_OK, curses.COLOR_GREEN, bg)
    curses.init_pair(CP_WARN, curses.COLOR_YELLOW, bg)
    curses.init_pair(CP_CRIT, curses.COLOR_RED, bg)
    curses.init_pair(CP_DIM, curses.COLOR_WHITE, bg)
    curses.init_pair(CP_ACCENT, curses.COLOR_CYAN, bg)
    curses.init_pair(CP_TRACE_TEMP, curses.COLOR_YELLOW, bg)
    curses.init_pair(CP_TRACE_POWER, curses.COLOR_CYAN, bg)
    curses.init_pair(CP_FOOTER, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(CP_TRACE_TOTAL, curses.COLOR_MAGENTA, bg)


def _temp_color(t):
    if t is None:
        return CP_DIM
    if t < 55.0:
        return CP_OK
    if t < 75.0:
        return CP_WARN
    return CP_CRIT


def _power_color(p, max_p):
    if p is None or max_p <= 0:
        return CP_DIM
    r = p / max_p
    if r < 0.5:
        return CP_OK
    if r < 0.8:
        return CP_WARN
    return CP_CRIT


def _clean(s):
    """Strip NULs and trailing whitespace from a C-origin string.

    Hailo's identify() returns fixed-width buffers that keep their
    trailing NUL padding in Python — passing these to curses.addstr
    raises ValueError: embedded null character.
    """
    if s is None:
        return None
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="replace")
    if not isinstance(s, str):
        s = str(s)
    nul = s.find("\x00")
    if nul != -1:
        s = s[:nul]
    return s.rstrip()


def _default_state_badge(val):
    """Default state-flag formatter: binary OK / ALERT / -- mapping.

    Returns `(text, color_pair_id, attrs)`. The renderer always layers
    `A_BOLD` on top of returned attrs. Used when a `state_flags` entry
    has no per-flag formatter (the 2-tuple short form).
    """
    if val is None:
        return " -- ", CP_DIM, 0
    if val == 0:
        return " OK ", CP_OK, 0
    return "ALERT", CP_CRIT, 0


def _on_off_white_badge(val):
    """White ON / OFF formatter for de-emphasized feature-enable flags.

    Returns plain-white text (`CP_DIM`) with no `A_BOLD` and no extra
    attrs — the rendering caller is expected to detect this via the
    entry's `plain` flag (4-tuple `state_flags` form) and skip both
    brackets and the implicit `A_BOLD` overlay. Used for Hailo's
    `TEMP_PROT` and `OCP_PROT` once we noticed they normally read ON
    at idle and the ON/OFF transition is rare enough to not deserve
    the loud bracketed-badge styling — they read as low-key trailing
    text rather than alarm indicators.

    Widths: `ON ` (3 chars with trailing space) and `OFF` (3 chars) so
    consecutive plain entries align visually.
    """
    if val is None:
        return "-- ", CP_DIM, 0
    if val != 0:
        return "ON ", CP_DIM, 0
    return "OFF", CP_DIM, 0


def _on_off_badge(val):
    """ON / OFF formatter for feature-enable flags where `1 = ON = green`
    and `0 = OFF = red`.

    Used for Hailo's `overcurrent_throttling_active` and
    `temperature_throttling_active`, both of which the SDK names
    suggest are latches but empirically are feature-enable bits
    (toggling `set_throttling_state(False)` flips both to 0). On a
    healthy idle chip they read `1` (= ON, protection enabled); a
    `0` reading (= OFF) means someone has explicitly disabled the
    protection — which is the genuinely-dangerous state worth
    surfacing in red.

    Text widths: "ON " and "OFF" are both 3 chars so the bracketed
    badges render at uniform width `[ON ]` / `[OFF]` for alignment.
    """
    if val is None:
        return " -- ", CP_DIM, 0
    if val != 0:
        return "ON ", CP_OK, 0
    return "OFF", CP_CRIT, 0


def _hailo_thermal_badge(val):
    """5-state THERMAL ladder for Hailo's `current_temperature_throttling_level`.

    The firmware reports −1 when no throttling is active, then four
    graduated tiers as the chip heats up:
      −1 → ` OK ` (green)            — full clock (requested freq)
       0 → ` L1 ` (yellow)           — first throttle at red_orange threshold
       1 → ` L2 ` (magenta)          — escalating
       2 → ` L3 ` (red)              — escalating
       3 → ` MAX` (red + reverse)    — top tier; absolute red threshold sits above

    Curses only has 8 base colors, so we substitute magenta for the
    "orange" mid-tier — visually distinct from yellow (L1) below and
    red (L3) above. MAX uses reverse video to stand apart from L3.
    """
    if val is None:
        return " -- ", CP_DIM, 0
    if val < 0:
        return " OK ", CP_OK, 0
    if val == 0:
        return " L1 ", CP_WARN, 0
    if val == 1:
        return " L2 ", CP_TRACE_TOTAL, 0    # magenta
    if val == 2:
        return " L3 ", CP_CRIT, 0
    # val >= 3 — top tier and beyond
    return " MAX", CP_CRIT, curses.A_REVERSE


def _format_state_badge(formatter, val):
    """Dispatch to a per-flag formatter or the default. Always returns
    a 3-tuple `(text, color, attrs)` — formatters that return a 2-tuple
    are zero-padded for backward-compat."""
    if formatter is None:
        return _default_state_badge(val)
    try:
        result = formatter(val)
    except Exception:
        return _default_state_badge(val)
    if isinstance(result, tuple):
        if len(result) == 3:
            return result
        if len(result) == 2:
            text, color = result
            return text, color, 0
    # Misbehaving formatter — fall back rather than raising.
    return _default_state_badge(val)


def _safe_addstr(win, y, x, s, attr=0):
    """addstr that never raises on boundary-reached writes."""
    if y < 0 or x < 0:
        return
    try:
        h, w = win.getmaxyx()
        if y >= h:
            return
        avail = w - x
        if avail <= 0:
            return
        if "\x00" in s:
            s = _clean(s)
        win.addnstr(y, x, s, avail, attr)
    except curses.error:
        pass
    except ValueError:
        pass


class TUI:
    def __init__(self, probes, interval=1.0, time_range=720.0,
                 graph_rows=None, power_max=10.0, temp_max=100.0,
                 csv_file=None):
        self.probes = probes
        self.interval = interval
        self.time_range = float(time_range)
        self.user_graph_rows = graph_rows
        self.TEMP_MAX = float(temp_max)
        self.POWER_MAX = float(power_max)
        self.last_poll = 0.0
        self.help_on = False
        self.start_ts = time.time()
        self._csv_file = csv_file
        self._csv_header_written = False
        self._csv_settle_polls = 0
        self._CSV_SETTLE_MAX_POLLS = 10

    def run(self, stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(100)  # getch() polls at 10Hz
        _init_colors()

        self._poll_all()

        while True:
            now = time.time()
            if now - self.last_poll >= self.interval:
                self._poll_all()
                self.last_poll = now

            stdscr.erase()
            self._draw(stdscr)
            stdscr.refresh()
            if self.help_on:
                self._draw_help_overlay(stdscr)

            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1

            if ch in (ord("q"), ord("Q"), 27):  # q/ESC
                break
            elif ch in (ord("r"), ord("R")):
                for p in self.probes:
                    p.reset_history()
            elif ch in (ord("h"), ord("H"), ord("?")):
                self.help_on = not self.help_on

    def _poll_all(self):
        for p in self.probes:
            p.poll()
        if self._csv_file is not None:
            self._csv_emit()

    def _csv_emit(self):
        """Append one row to the CSV file (write header on first call)."""
        if not self._csv_header_written:
            settled = all(getattr(p, "metrics_settled", True)
                          for p in self.probes)
            if not settled:
                self._csv_settle_polls += 1
                if self._csv_settle_polls < self._CSV_SETTLE_MAX_POLLS:
                    return
            cols = ["time"]
            for p in self.probes:
                for m in (getattr(p, "metrics", None) or []):
                    cols.append(f"{p.bdf}_{m.label}")
                # state_flags: extra per-flag columns (enum-valued —
                # e.g. THERMAL, POWER for MemryX). Not part of
                # `metrics` because they're state indicators, not
                # graphed time-series.
                # state_flags entries are (label, hist) or
                # (label, hist, formatter) — we only need the first
                # two elements for CSV purposes.
                for entry in (getattr(p, "state_flags", None) or []):
                    cols.append(f"{p.bdf}_{entry[0]}")
            try:
                self._csv_file.write(",".join(cols) + "\n")
            except (OSError, ValueError):
                self._csv_file = None
                return
            self._csv_header_written = True
        ts = datetime.now().isoformat(timespec="milliseconds")
        row = [ts]
        for p in self.probes:
            for m in (getattr(p, "metrics", None) or []):
                v = m.history[-1] if m.history else None
                row.append("" if v is None else f"{v:.6f}")
            for entry in (getattr(p, "state_flags", None) or []):
                hist = entry[1]
                v = hist[-1] if hist else None
                # Integer-valued enums; write without trailing zeros
                row.append("" if v is None else f"{int(v)}")
        try:
            self._csv_file.write(",".join(row) + "\n")
        except (OSError, ValueError):
            self._csv_file = None

    # -- drawing -------------------------------------------------------------

    def _draw(self, stdscr):
        h, w = stdscr.getmaxyx()
        self._draw_top_bar(stdscr, w)

        if not self.probes:
            _safe_addstr(stdscr, 2, 2,
                         "No devices found.", curses.color_pair(CP_CRIT))
            self._draw_footer(stdscr, h, w)
            return

        avail = max(0, h - 2)
        per_dev = max(3, avail // len(self.probes))
        y = 1
        for i, probe in enumerate(self.probes):
            self._draw_device(stdscr, y, w, per_dev, probe, i)
            y += per_dev
            if y >= h - 1:
                break

        self._draw_footer(stdscr, h, w)

    def _draw_top_bar(self, stdscr, w):
        tstamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        uptime = int(time.time() - self.start_ts)
        up_s = f"{uptime // 60:d}m{uptime % 60:02d}s"
        n = len(self.probes)
        open_ok = sum(1 for p in self.probes if p.device is not None)
        left = (f" mb-powermon — Power Monitor TUI   {tstamp}   uptime {up_s}"
                f"   {n} device(s) ({open_ok} open, {n - open_ok} error) ")
        pad = max(1, w - len(left))
        bar = (left + " " * pad)[:w]
        _safe_addstr(stdscr, 0, 0, bar, curses.color_pair(CP_TITLE))

    def _draw_footer(self, stdscr, h, w):
        """nvtop-style function-key bar at the bottom."""
        keys = [(" q", "Quit"), (" r", "Reset"), (" h", "Help")]
        col = 0
        y = h - 1
        _safe_addstr(stdscr, y, 0, " " * w, curses.color_pair(CP_FOOTER))
        for k, label in keys:
            kstr = f" {k.strip()} "
            lstr = f"{label} "
            if col + len(kstr) + len(lstr) > w:
                break
            _safe_addstr(stdscr, y, col, kstr,
                         curses.color_pair(CP_DIM) | curses.A_BOLD)
            col += len(kstr)
            _safe_addstr(stdscr, y, col, lstr,
                         curses.color_pair(CP_FOOTER))
            col += len(lstr)

    # -- per-device panel ----------------------------------------------------

    def _draw_device(self, stdscr, y0, width, slot_rows, probe, idx):
        """Render one device panel."""
        if slot_rows < 3:
            self._draw_one_liner(stdscr, y0, width, probe)
            return

        self._draw_identity(stdscr, y0, width, probe, idx)
        if slot_rows >= 2:
            self._draw_stats(stdscr, y0 + 1, width, probe)

        # Optional status row, ABOVE the inline bars so the per-probe
        # status info (CLOCK prefix + state-flag badges) sits next to
        # the identity / stats header rather than buried between the
        # bars and the graph. Reserved when the probe declares EITHER
        # a non-empty `state_flags` (e.g. Hailo / MemryX) OR a
        # `state_prefix` (e.g. Axelera / DeepX CLOCK with no flags).
        # Costs 1 row of graph height.
        flags = getattr(probe, "state_flags", None) or []
        has_prefix = bool(getattr(probe, "state_prefix", None))
        has_status = (bool(flags) or has_prefix) and slot_rows >= 4
        flag_row = 1 if has_status else 0
        if has_status:
            self._draw_state_flags(stdscr, y0 + 2, width, probe)

        if slot_rows >= 3 + flag_row:
            self._draw_inline_bars(stdscr, y0 + 2 + flag_row, width, probe)

        graph_y = y0 + 3 + flag_row
        graph_h = slot_rows - 3 - 1 - flag_row  # reserve 1 for time axis
        if graph_h < 3:
            return
        if self.user_graph_rows is not None:
            graph_h = max(3, min(graph_h, self.user_graph_rows))
        self._draw_graph_box(stdscr, graph_y, 0, width, graph_h, probe)
        time_y = graph_y + graph_h
        self._draw_time_axis(stdscr, time_y, 0, width, graph_h)

    def _draw_state_flags(self, stdscr, y, width, probe):
        """Render the state row as `CLOCK <value>  THERMAL [ OK  OK  OK  OK ]  POWER [ OK ]`.

        Format (left-aligned at column 0, lining up with identity /
        stats rows):

          <PREFIX_LABEL> <prefix_value>  <FLAG_LABEL> [<state> ...]  ...

        - Labels (`CLOCK`, `THERMAL`, `POWER`, ...) render bold-cyan
          (`CP_ACCENT`), matching the `PRODUCT / DESCRIPTION` style
          on the stats row above.
        - The optional prefix value (e.g. `850MHz`) renders dim.
        - Brackets `[` `]` render dim — pure scaffolding so the
          state badges inside pop out.
        - Each state badge renders bold:
            val == 0     → green ` OK `
            val != 0     → red   `ALERT`
            val is None  → dim   ` -- `
          ` OK ` and ` -- ` keep the spaces around them so a single
          `[ OK ]` doesn't look cramped next to `[ALERT]`.

        **Group-by-prefix**: state_flags entries whose labels match
        the pattern `PREFIX_<digit>` (e.g. `THERMAL_0`, `THERMAL_1`,
        `THERMAL_2`, `THERMAL_3`) collapse to a single widget with
        multiple per-chip badges inside one bracket:
          `THERMAL [ OK  OK  OK  OK ]`
        Each inner badge is colored independently from its chip's
        state. CSV still emits one column per per-chip flag.

        Prefix contract: `probe.state_prefix` is either `None`, a
        tuple `(label, value_str)`, or a zero-arg callable returning
        either of those. A bare string is accepted as a fallback —
        rendered as `STATUS <value>`.
        """
        flags = getattr(probe, "state_flags", None) or []
        prefix = getattr(probe, "state_prefix", None)
        if callable(prefix):
            try:
                prefix = prefix()
            except Exception:
                prefix = None
        if not flags and not prefix:
            return

        accent = curses.color_pair(CP_ACCENT) | curses.A_BOLD
        dim = curses.color_pair(CP_DIM)
        x = 0  # left-aligned, like identity / stats rows

        if prefix is not None:
            if isinstance(prefix, tuple) and len(prefix) == 2:
                p_label, p_value = prefix
            else:
                p_label, p_value = "STATUS", str(prefix)
            p_value = str(p_value)
            chunk = f"{p_label} "
            if x + len(chunk) + len(p_value) >= width:
                return
            _safe_addstr(stdscr, y, x, chunk, accent); x += len(chunk)
            _safe_addstr(stdscr, y, x, p_value, dim); x += len(p_value)

        # Group consecutive flags whose labels share a `PREFIX_<digit>`
        # form into one widget. Non-matching labels render alone.
        groups = self._group_state_flags(flags)

        for label, entries, plain in groups:
            states = [_format_state_badge(fmt, h[-1] if h else None)
                      for h, fmt in entries]
            sep = "  " if x > 0 else ""
            # Plain widgets skip the surrounding `[ ]` brackets entirely.
            # Width budget changes accordingly: "  LABEL s0  s1 ..."
            # rather than "  LABEL [s0  s1 ...]".
            head_len = (len(sep) + len(label) + 1
                        + (0 if plain else 1))  # "<sep><label> " [+ "["]
            inner_len = sum(len(s[0]) for s in states) \
                        + 2 * (len(states) - 1)  # two-space separators
            tail_len = 0 if plain else 1
            if x + head_len + inner_len + tail_len > width:
                break
            # Label always renders bold-cyan; the "(" / ")" bracket
            # styling and the bold-overlay on the value are what plain
            # mode suppresses.
            _safe_addstr(stdscr, y, x, f"{sep}{label} ", accent)
            x += len(sep) + len(label) + 1
            if not plain:
                _safe_addstr(stdscr, y, x, "[", dim); x += 1
            bold = 0 if plain else curses.A_BOLD
            for i, (text, color, attrs) in enumerate(states):
                if i > 0:
                    _safe_addstr(stdscr, y, x, "  ", dim); x += 2
                _safe_addstr(stdscr, y, x, text,
                             curses.color_pair(color) | bold | attrs)
                x += len(text)
            if not plain:
                _safe_addstr(stdscr, y, x, "]", dim); x += 1

    _GROUP_RE = re.compile(r"^(.*)_(\d+)$")

    def _group_state_flags(self, flags):
        """Collapse consecutive `PREFIX_<digit>` labels into a single
        widget. Returns a list of `(display_label, [(hist, formatter), ...], plain)`
        — non-grouped labels yield a single-element list. Each
        `state_flags` entry may be:
        - `(label, hist)` — 2-tuple, binary OK/ALERT via default
          formatter (`formatter=None` here, `plain=False`).
        - `(label, hist, formatter)` — 3-tuple, custom formatter,
          standard bracketed-and-bold rendering.
        - `(label, hist, formatter, plain)` — 4-tuple. When
          `plain=True` the renderer skips the surrounding `[ ]`
          brackets AND skips the implicit `A_BOLD` overlay, producing
          de-emphasized inline text. Used with `_on_off_white_badge`
          for Hailo's `TEMP_PROT` / `OCP_PROT`.

        For grouped entries (THERMAL_0..THERMAL_3 etc.), the group's
        `plain` is the OR of its entries' `plain` — in practice every
        entry in a group either is or isn't plain, since they share a
        formatter; this is just defensive."""
        def _unpack(entry):
            if len(entry) == 2:
                return entry[0], entry[1], None, False
            if len(entry) == 3:
                return entry[0], entry[1], entry[2], False
            return entry[0], entry[1], entry[2], bool(entry[3])

        groups = []
        i = 0
        n = len(flags)
        while i < n:
            label, hist, fmt, plain = _unpack(flags[i])
            m = self._GROUP_RE.match(label)
            if m:
                prefix = m.group(1)
                pairs = [(hist, fmt)]
                group_plain = plain
                j = i + 1
                while j < n:
                    label2, hist2, fmt2, plain2 = _unpack(flags[j])
                    m2 = self._GROUP_RE.match(label2)
                    if m2 and m2.group(1) == prefix:
                        pairs.append((hist2, fmt2))
                        group_plain = group_plain or plain2
                        j += 1
                    else:
                        break
                groups.append((prefix, pairs, group_plain))
                i = j
            else:
                groups.append((label, [(hist, fmt)], plain))
                i += 1
        return groups

    def _draw_identity(self, stdscr, y, width, probe, idx):
        """Line 1: `Device N [BDF Board HAILO8]  PCIe x4/x4 @ 8.0GT/s`."""
        ident = probe.identity
        board = ident.get("board_name") or "Hailo"
        arch = ident.get("arch")
        arch_s = str(arch).split(".")[-1] if arch is not None else ""

        pcie = probe.pcie
        cur_w = pcie.get("current_link_width") or "?"
        max_w = pcie.get("max_link_width") or "?"
        cur_s = pcie.get("current_link_speed") or "?"
        cur_s = cur_s.replace(" PCIe", "").replace("PCIe", "").strip() or "?"
        link = f"x{cur_w}/x{max_w} @ {cur_s}"

        link_attr = curses.color_pair(CP_DIM)
        try:
            if int(max_w) > int(cur_w):
                link_attr = curses.color_pair(CP_WARN) | curses.A_BOLD
        except (TypeError, ValueError):
            pass

        accent = curses.color_pair(CP_ACCENT) | curses.A_BOLD
        dim = curses.color_pair(CP_DIM)

        x = 0
        s = f"Device {idx} "
        _safe_addstr(stdscr, y, x, s, accent); x += len(s)

        def _norm(s):
            return "".join(c for c in (s or "").upper() if c.isalnum())
        bracket = f"[{probe.bdf}  {board}"
        if arch_s and _norm(arch_s) != _norm(board):
            bracket += f"  {arch_s}"
        bracket += "]"
        _safe_addstr(stdscr, y, x, bracket, dim | curses.A_BOLD); x += len(bracket)

        label_cp = curses.color_pair(CP_ACCENT) | curses.A_BOLD
        crit = curses.color_pair(CP_CRIT) | curses.A_BOLD

        if pcie.get("current_link_width") or pcie.get("max_link_width"):
            _safe_addstr(stdscr, y, x, "  PCIe ", label_cp); x += 7
            _safe_addstr(stdscr, y, x, link, link_attr); x += len(link)

        aspm = pcie.get("aspm")
        aspm_err = pcie.get("aspm_error")
        if aspm or aspm_err:
            _safe_addstr(stdscr, y, x, "  ASPM ", label_cp); x += 7
            if aspm:
                _safe_addstr(stdscr, y, x, aspm, dim); x += len(aspm)
            else:
                s = f"<{aspm_err}>"
                _safe_addstr(stdscr, y, x, s, crit); x += len(s)

    def _draw_stats(self, stdscr, y, width, probe):
        """Stats line: `PRODUCT <p>  DESCRIPTION <d>  PART <pn>  SERIAL <s>`."""
        if probe.device is None:
            x = 0
            s = "ERROR"
            _safe_addstr(stdscr, y, x, s,
                         curses.color_pair(CP_CRIT) | curses.A_BOLD)
            x += len(s)
            if probe.error:
                s = f" ({probe.error})"
                _safe_addstr(stdscr, y, x, s, curses.color_pair(CP_CRIT))
            return

        dim = curses.color_pair(CP_DIM)
        label_cp = curses.color_pair(CP_ACCENT) | curses.A_BOLD
        ident = probe.identity
        fields = [
            ("PRODUCT",     ident.get("product_name")),
            ("DESCRIPTION", ident.get("description")),
            ("PART",        ident.get("part_number")),
            ("SERIAL",      ident.get("serial")),
        ]
        x = 0
        for label, value in fields:
            if not value:
                continue
            sep = "  " if x > 0 else ""
            chunk = f"{sep}{label} "
            if x + len(chunk) + len(value) >= width:
                break
            _safe_addstr(stdscr, y, x, chunk, label_cp); x += len(chunk)
            _safe_addstr(stdscr, y, x, value, dim); x += len(value)

    def _metric_max(self, probe, metric):
        """Pick the axis cap for a metric.

        Priority:
          1. metric.max_val if set per-metric (e.g. PMD2 TOTAL = 100 W,
             which is legitimately a different scale from PCIE rails)
          2. The TUI's TEMP_MAX / POWER_MAX, which the user controls
             via --temp-max / --power-max.

        Probe-class POWER_MAX is intentionally NOT consulted —
        otherwise a probe-class default would shadow the user's
        explicit CLI flag, which is exactly what users don't expect
        from a `--power-max` argument.
        """
        if metric.max_val is not None:
            return metric.max_val
        if metric.unit == "°C":
            return self.TEMP_MAX
        if metric.unit == "W":
            return self.POWER_MAX
        return 100.0

    def _draw_inline_bars(self, stdscr, y, width, probe):
        """Inline bars row, one per metric the probe declares."""
        metrics = getattr(probe, "metrics", None) or []
        if not metrics:
            return
        n = len(metrics)
        gap = 2  # spaces between adjacent bars
        avail = width - gap * (n - 1)
        slot = avail // n
        x = 0
        for i, m in enumerate(metrics):
            w = slot if i < n - 1 else width - x
            val = m.history[-1] if m.history else None
            attr = curses.color_pair(m.color_id) | curses.A_BOLD
            self._draw_one_bar(stdscr, y, x, w, m.label, val,
                               self._metric_max(probe, m), m.unit, attr)
            x += w + gap

    def _draw_one_bar(self, stdscr, y, x, width, label, val, max_val, unit,
                      label_attr):
        """Render an inline bar in nvtop's style."""
        if width < len(label) + 5:
            return
        dim = curses.color_pair(CP_DIM)
        if val is None:
            text = f"--{unit}/{max_val:.0f}{unit}"
        elif unit == "°C" or unit == "%":
            text = f"{val:.0f}{unit}/{max_val:.0f}{unit}"
        else:
            text = f"{val:.2f}{unit}/{max_val:.0f}{unit}"

        interior_w = width - len(label) - 2
        if interior_w < 1:
            return
        if len(text) > interior_w:
            text = text[-interior_w:]
        padded = text.rjust(interior_w)

        if val is None or max_val <= 0:
            fill_n = 0
        else:
            ratio = max(0.0, min(1.0, val / max_val))
            fill_n = int(round(ratio * interior_w))

        _safe_addstr(stdscr, y, x, label, label_attr); x += len(label)
        _safe_addstr(stdscr, y, x, "[", dim | curses.A_BOLD); x += 1
        if fill_n > 0:
            _safe_addstr(stdscr, y, x, padded[:fill_n],
                         label_attr | curses.A_REVERSE)
        if fill_n < interior_w:
            _safe_addstr(stdscr, y, x + fill_n, padded[fill_n:], dim)
        x += interior_w
        _safe_addstr(stdscr, y, x, "]", dim | curses.A_BOLD)

    def _draw_one_liner(self, stdscr, y, width, probe):
        pcie = probe.pcie
        cur_w = pcie.get("current_link_width") or "?"
        max_w = pcie.get("max_link_width") or "?"
        link = f"x{cur_w}/x{max_w}"
        bits = [f"[{probe.bdf}]", f"{link:>8}"]
        for m in (getattr(probe, "metrics", None) or []):
            v = m.history[-1] if m.history else None
            if v is None:
                bits.append(f"{m.label}= --{m.unit}")
            elif m.unit == "°C":
                bits.append(f"{m.label}={v:5.1f}{m.unit}")
            else:
                bits.append(f"{m.label}={v:4.2f}{m.unit}")
        line = "  ".join(bits)
        _safe_addstr(stdscr, y, 0, line[:width])

    # -- graph box -----------------------------------------------------------

    BORDER = {
        "tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
        "h": "─", "v": "│",
    }

    YLABEL_W = 4  # "100 "

    def _draw_graph_box(self, stdscr, y0, x0, width, height, probe):
        """Bordered plot box with dual step-line traces (TEMP + POWER)."""
        if width < 10 or height < 3:
            return
        box_x = x0 + self.YLABEL_W
        box_w = width - self.YLABEL_W
        inner_w = box_w - 2
        inner_h = height - 2
        if inner_w < 2 or inner_h < 1:
            return
        border_cp = curses.color_pair(CP_DIM)

        top = (self.BORDER["tl"] + self.BORDER["h"] * inner_w
               + self.BORDER["tr"])
        _safe_addstr(stdscr, y0, box_x, top, border_cp)

        for r in range(inner_h):
            _safe_addstr(stdscr, y0 + 1 + r, box_x,
                         self.BORDER["v"], border_cp)
            _safe_addstr(stdscr, y0 + 1 + r, box_x + box_w - 1,
                         self.BORDER["v"], border_cp)

        drawn = set()
        for v in (100, 75, 50, 25, 0):
            frac = v / 100.0
            row = y0 + 1 + int(round((1.0 - frac) * (inner_h - 1)))
            if row in drawn:
                continue
            drawn.add(row)
            lbl = f"{v:>3} "
            _safe_addstr(stdscr, row, x0, lbl, border_cp)

        bottom = (self.BORDER["bl"] + self.BORDER["h"] * inner_w
                  + self.BORDER["br"])
        _safe_addstr(stdscr, y0 + height - 1, box_x, bottom, border_cp)

        plot_x = box_x + 1
        plot_w = inner_w
        plot_y = y0 + 1
        plot_h = inner_h

        if plot_w < 2 or plot_h < 1:
            return

        metrics = getattr(probe, "metrics", None) or []
        for m in reversed(metrics):
            attr = curses.color_pair(m.color_id) | curses.A_BOLD
            self._plot_step(stdscr, plot_y, plot_x, plot_w, plot_h,
                            m.history,
                            self._metric_max(probe, m), attr)

        for i, m in enumerate(metrics):
            row = plot_y + i
            if row >= plot_y + plot_h:
                break
            mx = self._metric_max(probe, m)
            mx_s = (f"{int(mx)}" if abs(mx - round(mx)) < 1e-6
                    else f"{mx:.1f}")
            if m.unit == "%":
                text = f"{m.label}/{mx_s}{m.unit}"
            else:
                text = f"{m.label}/{mx_s}{m.unit} %"
            if len(text) > plot_w:
                continue
            attr = curses.color_pair(m.color_id) | curses.A_BOLD
            _safe_addstr(stdscr, row, plot_x, text, attr)

    def _plot_step(self, stdscr, y0, x0, width, height, series, max_val,
                   attr):
        """Draw a step-line trace. Newest sample goes on the rightmost col."""
        if width <= 0 or height <= 0 or max_val <= 0 or not series:
            return

        samples = list(series)[-width:]
        pad = width - len(samples)
        samples = [None] * pad + samples

        def row_for(val):
            if val is None:
                return None
            norm = max(0.0, min(1.0, val / max_val))
            return int(round((1.0 - norm) * (height - 1)))

        prev = None
        for c in range(width):
            cur = row_for(samples[c])
            if cur is None:
                prev = None
                continue
            if prev is not None and prev != cur:
                lo = min(prev, cur)
                hi = max(prev, cur)
                for rr in range(lo + 1, hi):
                    _safe_addstr(stdscr, y0 + rr, x0 + c,
                                 self.BORDER["v"], attr)
                if prev < cur:
                    _safe_addstr(stdscr, y0 + prev, x0 + c,
                                 self.BORDER["tr"], attr)
                    _safe_addstr(stdscr, y0 + cur, x0 + c,
                                 self.BORDER["bl"], attr)
                else:
                    _safe_addstr(stdscr, y0 + prev, x0 + c,
                                 self.BORDER["br"], attr)
                    _safe_addstr(stdscr, y0 + cur, x0 + c,
                                 self.BORDER["tl"], attr)
            else:
                _safe_addstr(stdscr, y0 + cur, x0 + c,
                             self.BORDER["h"], attr)
            prev = cur

    def _draw_time_axis(self, stdscr, y, x0, width, graph_h):
        """Time labels below the graph box."""
        if y < 0:
            return
        box_x = x0 + self.YLABEL_W
        box_w = width - self.YLABEL_W
        inner_w = box_w - 2
        if inner_w <= 0:
            return
        plot_x = box_x + 1
        plot_w = inner_w
        span_s = int(plot_w * self.interval)
        n_ticks = 5 if plot_w >= 60 else 4 if plot_w >= 30 else 3
        dim = curses.color_pair(CP_DIM)
        for i in range(n_ticks):
            frac = i / (n_ticks - 1)
            col = int(round(frac * (plot_w - 1)))
            t_sec = int(round((1.0 - frac) * span_s))
            lbl = f"{t_sec:d}s"
            if i == 0:
                start = plot_x + col
            elif i == n_ticks - 1:
                start = plot_x + col - len(lbl) + 1
            else:
                start = plot_x + col - len(lbl) // 2
            _safe_addstr(stdscr, y, start, lbl, dim)

    # -- help overlay --------------------------------------------------------

    def _draw_help_overlay(self, stdscr):
        lines = [
            "  mb-powermon  —  Power Monitor TUI",
            "",
            "  Controls",
            "    q / ESC    quit",
            "    r          reset history",
            "    h / ?      toggle this help",
            "",
            "  Metrics",
            "    Hailo:  chip temp + INA231 firmware-averaged power",
            "       (256 samples @ 1.1ms via Device.control)",
            "    Metis:  per-core temp via triton_trace --peek",
            "       (power not exposed on Metis M.2)",
            "    DeepX:  per-NPU temp via dxrt-cli -s",
            "       (power not exposed on M1)",
            "    MemryX: per-MPU temp via Linux hwmon (memx0)",
            "       (power not exposed via hwmon — use INA228)",
            "    PMD2:   PCIE1/2/3 + TOTAL rail power (USB CDC)",
            "    Adafruit: 1–4× INA228 power monitors over Adafruit",
            "       FT232H USB→I2C bridge (one POW trace per",
            "       sensor; V/I in --once / --csv)",
            "    PCIe link + ASPM from sysfs config space",
            "",
            "  An 'ERROR' marker means the device couldn't be opened",
            "  (held by another process or vendor SDK not installed).",
            "  Sysfs PCIe info still displays normally.",
            "",
            "  Press any key to close.",
        ]
        h, w = stdscr.getmaxyx()
        box_h = len(lines) + 2
        box_w = max(len(s) for s in lines) + 4
        if box_h > h or box_w > w:
            return
        y0 = (h - box_h) // 2
        x0 = (w - box_w) // 2
        try:
            win = curses.newwin(box_h, box_w, y0, x0)
            win.box()
            for i, s in enumerate(lines):
                win.addnstr(1 + i, 2, s, box_w - 4)
            win.refresh()
        except curses.error:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=("NVTOP-style TUI monitor for AI accelerators "
                     "(Hailo + Axelera + PMD2 + INA228): temperature, "
                     "power, PCIe link."),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--interval", type=float, default=1.0,
        help="Seconds between telemetry polls (graph X-axis resolution)")
    parser.add_argument(
        "--time-range", type=float, default=720.0, metavar="SEC",
        help=("History retention window, in seconds. The per-metric "
              "deque is sized to hold ceil(time_range / interval) "
              "samples. The visible X-axis span depends on terminal "
              "width: the plot draws one sample per column at the "
              "polling interval, so it shows plot_w × interval seconds "
              "(typically 60-200 s on a default-width terminal). Use "
              "--time-range to control how far back samples are kept "
              "in memory; use --interval to control X-axis density. "
              "Default: 720 (12 min)."))
    parser.add_argument(
        "--device", action="append", default=None, metavar="ID",
        help=("Limit monitoring to this device ID (PCI BDF for Hailo/"
              "Axelera/DeepX/MemryX, e.g. 0000:c6:00.0; serial port "
              "path for PMD2, e.g. /dev/ttyACM0). Repeat for multiple. "
              "Default: all."))
    def _probe_list(s):
        valid = {"hailo", "axelera", "deepx", "memryx", "elmorlabs",
                 "adafruit"}
        items = [tok.strip().lower() for tok in s.split(",") if tok.strip()]
        if not items:
            raise argparse.ArgumentTypeError(
                "--probe expects a comma-separated list, e.g. "
                "'hailo,elmorlabs'")
        bad = [t for t in items if t not in valid]
        if bad:
            raise argparse.ArgumentTypeError(
                f"unknown probe name(s): {', '.join(bad)} "
                f"(allowed: {', '.join(sorted(valid))})")
        return items

    def _addr_list(s):
        """Parse a comma-separated list of I2C addresses (hex/dec/oct)."""
        out = []
        for tok in s.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                out.append(int(tok, 0))
            except ValueError:
                raise argparse.ArgumentTypeError(
                    f"bad I2C address {tok!r} — expected hex (0x40), "
                    f"decimal (64), or octal (0o100)")
        if not out:
            raise argparse.ArgumentTypeError(
                "--ina228-addresses expects at least one address")
        return tuple(out)

    parser.add_argument(
        "--probe", type=_probe_list, default=None, metavar="LIST",
        help=("Restrict discovery to a comma-separated list of probe "
              "types — any of: hailo, axelera, deepx, memryx, elmorlabs "
              "(PMD2), adafruit (Adafruit INA228 sensors via FT232H "
              "USB→I2C bridge). The list ORDER also controls the on-screen "
              "panel order: e.g. --probe adafruit,hailo puts the "
              "INA228 ground-truth panel above the Hailo chip's "
              "self-report. Default: all probe types scanned, with "
              "adafruit listed first (so external power readings sit "
              "above chip-side telemetry)."))
    parser.add_argument(
        "--ina228-shunt", type=float, default=0.015, metavar="OHM",
        help=("Shunt resistance for INA228 calibration (ohms). The "
              "Adafruit INA228 breakout has a 0.015 Ω on-board shunt; "
              "override if you wire an external one. Shared across all "
              "INA228s on the bus."))
    parser.add_argument(
        "--ina228-max-current", type=float, default=5.0, metavar="A",
        help=("Full-scale current for INA228 calibration (amps). "
              "Determines the per-LSB current resolution — smaller "
              "numbers give cleaner readings within their range. "
              "When the actual current exceeds this calibration, the "
              "INA228 latches its current-overflow flag and `power` "
              "reads as 0.0 W until the flag clears — a reading of "
              "exactly 0.0 W during a high-load workload usually "
              "means you need to raise this value. "
              "Default 5 A is sized for Hailo-8 / Metis M.2 (peak ~1.5 A "
              "and ~4.5 A respectively on the 3.3 V rail) with ~10%% "
              "headroom. **Use at least 10 A for MemryX MX3 in 20T "
              "boost mode** — steady-state is ~4 A but transient inrush "
              "during ramp-up exceeds 5 A and triggers the overflow "
              "latch. The 0.015 Ω shunt's hard ceiling is ~10.9 A "
              "(±163.84 mV input range / 0.015 Ω); beyond that the "
              "analog front-end clips. Shared across all INA228s "
              "on the bus."))
    parser.add_argument(
        "--ina228-addresses", type=_addr_list, default=None,
        metavar="LIST",
        help=("Comma-separated INA228 I2C addresses to probe (hex, "
              "dec, or oct). Default: scans all four canonical "
              "addresses 0x40,0x41,0x44,0x45 — only those that "
              "respond to an i2c.scan() are instantiated. The "
              "INA228's A0/A1 strap pins select among these four."))
    parser.add_argument(
        "--graph-rows", type=int, default=None,
        help=("Number of rows per time-series graph. Default: auto-size "
              "to terminal height."))
    parser.add_argument(
        "--power-max", type=float, default=10.0,
        help=("Upper bound (in watts) for the power bar/axis. Hailo-8 M.2 "
              "peaks near 2.5W and Hailo-8 PCIe AIC near 5W, so 10W leaves "
              "headroom. Raise for higher-TDP accelerators, or lower for "
              "a tighter axis on idle-monitoring."))
    parser.add_argument(
        "--temp-max", type=float, default=100.0,
        help=("Upper bound (in °C) for the temperature bar/axis. Hailo-8 "
              "throttles near 85°C; the 100°C default leaves some headroom."))
    parser.add_argument(
        "--once", action="store_true",
        help="Print a single snapshot to stdout instead of starting the TUI.")
    parser.add_argument(
        "--csv", type=str, default=None, metavar="FILE",
        help=("Append telemetry to FILE as CSV (one header row, then "
              "one value row per --interval) WHILE the TUI runs in "
              "the foreground. Existing FILE is overwritten. Output "
              "is line-buffered, so the file stays current under "
              "`tail -f`. With --once, writes the header plus a single "
              "snapshot row instead of streaming."))
    parser.add_argument(
        "--active-only", action="store_true",
        help=("Hide devices whose vendor SDK couldn't be opened (i.e. "
              "those that would render with an ERROR badge). PCIe-only "
              "devices remain visible by default; this flag drops them."))
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help=("Log probe-init diagnostics to stderr (SDK availability, "
              "triton_trace path, collector enable result, persistent "
              "--peek failures). Useful when temperatures aren't showing "
              "up and you need to see which step is failing."))
    return parser.parse_args(argv)


def do_snapshot(probes):
    """Single-shot plain-text readout (for piping, cron, CI)."""
    for p in probes:
        p.poll()
    for p in probes:
        ident = p.identity
        pcie = p.pcie
        cur_w = pcie.get("current_link_width") or "?"
        max_w = pcie.get("max_link_width") or "?"
        cur_s = pcie.get("current_link_speed") or "?"
        max_s = pcie.get("max_link_speed") or "?"
        print(f"[{p.bdf}]")
        if ident:
            arch = ident.get("arch")
            arch_s = str(arch).split(".")[-1] if arch else "?"
            print(f"    BOARD     = {ident.get('board_name') or '?'}"
                  f"  ARCH={arch_s}")
            if ident.get("product_name"):
                print(f"    PRODUCT   = {ident['product_name']}")
            if ident.get("description"):
                print(f"    DESC      = {ident['description']}")
            if ident.get("part_number"):
                print(f"    PART      = {ident['part_number']}")
            if ident.get("serial"):
                print(f"    SERIAL    = {ident['serial']}")
        if pcie:
            print(f"    PCIE      = x{cur_w}/x{max_w} @ {cur_s} "
                  f"(max {max_s})")
        aspm = pcie.get("aspm")
        aspm_err = pcie.get("aspm_error")
        if aspm:
            print(f"    ASPM      = {aspm}")
        elif aspm_err:
            print(f"    ASPM      = <{aspm_err}>")
        busy = "" if p.device is not None else "  [ERROR]"
        for m in (getattr(p, "metrics", None) or []):
            v = m.history[-1] if m.history else None
            v_s = (f"{v:.2f}{m.unit}" if v is not None else "n/a")
            tag = busy if (m is p.metrics[-1]) else ""
            # Metric labels are stored uppercase ("POW", "TS0", ...) so
            # we use them verbatim — no .lower() needed for CAPS output.
            print(f"    {m.label:<10}= {v_s}{tag}")
        snap = getattr(p, "last_snapshot", None)
        # MemryX has its own block below that prints `CLOCK = X MHz`,
        # so suppress the generic state_prefix / state_flags renderers
        # for it to avoid duplication.
        memryx_block = bool(snap and "thermal_states" in snap)
        # Generic state_prefix printer — surfaces the `(label, value)`
        # the probe renders before its state-flag badges on the TUI
        # status row. For Hailo: `CLOCK = 400MHz` (current effective
        # NN clock, drops with the throttle level).
        prefix = getattr(p, "state_prefix", None)
        if callable(prefix):
            try:
                prefix = prefix()
            except Exception:
                prefix = None
        if prefix and not memryx_block:
            if isinstance(prefix, tuple) and len(prefix) == 2:
                p_label, p_value = prefix
            else:
                p_label, p_value = "STATUS", str(prefix)
            # state_prefix labels are stored uppercase ("CLOCK") — emit
            # verbatim, force-upper for safety in case a probe slips a
            # lowercase string in.
            print(f"    {str(p_label).upper():<10}= {p_value}")
        # Generic state_flags printer — runs for probes that DON'T have
        # a custom last_snapshot rendering block below (e.g. Hailo:
        # THERMAL / TEMP_PROT / OCP / OCP_PROT). Uses each flag's
        # formatter to get the badge text so the --once output matches
        # what the TUI shows. Skipped for MemryX (which has its own
        # per-chip block via snap["thermal_states"]).
        sflags = getattr(p, "state_flags", None) or []
        if sflags and not memryx_block:
            for entry in sflags:
                label = entry[0]
                hist = entry[1]
                fmt = entry[2] if len(entry) >= 3 else None
                v = hist[-1] if hist else None
                text, _color, _attrs = _format_state_badge(fmt, v)
                v_s = "n/a" if v is None else str(int(v))
                # state_flags labels are stored uppercase — force-upper
                # for safety.
                print(f"    {label.upper():<10}= {v_s} ({text.strip()})")
        # MemryX schema: voltage + frequency + per-chip thermal
        # throttle states + module-level power-alert
        if snap and ("thermal_states" in snap or "power_alert" in snap):
            volt = snap.get("voltage_mv")
            freq = snap.get("frequency_mhz")
            if volt is not None:
                print(f"    VOLT      = {volt} mV")
            if freq is not None:
                print(f"    CLOCK     = {freq:.0f} MHz")
            ts_list = snap.get("thermal_states")
            pa = snap.get("power_alert")
            if ts_list:
                tags = ["OK" if v == 0 else "ALERT" for v in ts_list]
                pretty = " ".join(f"chip{i}:{int(v)}({t})"
                                  for i, (v, t) in enumerate(zip(ts_list, tags)))
                print(f"    THERMAL   = {pretty}")
            if pa is not None:
                state = "OK" if pa == 0 else "ALERT"
                print(f"    PWR_ALERT = {int(pa)} ({state})")
        if snap and "rails" in snap:
            for r in snap["rails"]:
                print(f"    RAIL {r['name']:<6}= "
                      f"{r['voltage_v']:5.2f}V  "
                      f"{r['current_a']:6.3f}A  "
                      f"{r['power_w']:6.2f}W")
            print(f"    AGGREGATE = EPS:{snap['eps_w']}W  "
                  f"PCIE:{snap['pcie_w']}W  "
                  f"ATX24:{snap['atx24_w']:.1f}W  "
                  f"TOT:{snap['total_w']}W")
        if snap and "sensors" in snap:
            addr_to_index = getattr(p, "ADDR_TO_INDEX", {})
            for s in snap["sensors"]:
                v_s = (f"{s['voltage_v']:5.2f}V" if s["voltage_v"] is not None
                       else "  --V")
                i_s = (f"{s['current_a']:6.3f}A" if s["current_a"] is not None
                       else "    --A")
                p_s = (f"{s['power_w']:6.2f}W" if s["power_w"] is not None
                       else "    --W")
                tag = f"P{addr_to_index.get(s['addr'], '?')} (0x{s['addr']:02X})"
                print(f"    {tag:<14}= {v_s}  {i_s}  {p_s}")
        if p.error:
            print(f"    ERROR     = {p.error}")
        print()


def write_csv_snapshot(csv_file, probes):
    """Write one CSV header + one CSV row to `csv_file`.

    Same schema as the TUI's `_csv_emit` (one column per metric, plus
    one column per state_flag) — keep these two paths in sync.
    """
    cols = ["time"]
    for p in probes:
        for m in (getattr(p, "metrics", None) or []):
            cols.append(f"{p.bdf}_{m.label}")
        for entry in (getattr(p, "state_flags", None) or []):
            cols.append(f"{p.bdf}_{entry[0]}")
    csv_file.write(",".join(cols) + "\n")
    ts = datetime.now().isoformat(timespec="milliseconds")
    row = [ts]
    for p in probes:
        for m in (getattr(p, "metrics", None) or []):
            v = m.history[-1] if m.history else None
            row.append("" if v is None else f"{v:.6f}")
        for entry in (getattr(p, "state_flags", None) or []):
            hist = entry[1]
            v = hist[-1] if hist else None
            row.append("" if v is None else f"{int(v)}")
    csv_file.write(",".join(row) + "\n")


def main(argv=None):
    args = parse_args(argv)

    locale.setlocale(locale.LC_ALL, "")

    # Probe display order. When --probe is given, the order in that
    # list also controls the order panels stack on screen — so e.g.
    # `--probe adafruit,hailo` puts the external INA228 panel on top
    # and the chip's self-report below it. Without --probe we use
    # DEFAULT_PROBE_ORDER, which leads with `adafruit` so the
    # ground-truth power measurement reads at the top of the screen.
    DEFAULT_PROBE_ORDER = ["adafruit", "hailo", "axelera", "deepx",
                           "memryx", "elmorlabs"]
    order = list(args.probe) if args.probe else DEFAULT_PROBE_ORDER
    enabled = set(order)

    hailo_bdfs = scan_hailo_devices() if "hailo" in enabled else []
    axelera_pairs = scan_axelera_devices() if "axelera" in enabled else []
    deepx_bdfs = scan_deepx_devices() if "deepx" in enabled else []
    memryx_bdfs = scan_memryx_devices() if "memryx" in enabled else []
    pmd2_ports = scan_pmd2_devices() if "elmorlabs" in enabled else []
    adafruit_ids = scan_adafruit_devices() if "adafruit" in enabled else []
    if args.device:
        wanted = set(args.device)
        hailo_bdfs = [b for b in hailo_bdfs if b in wanted]
        axelera_pairs = [(b, s) for (b, s) in axelera_pairs if b in wanted]
        deepx_bdfs = [b for b in deepx_bdfs if b in wanted]
        memryx_bdfs = [b for b in memryx_bdfs if b in wanted]
        pmd2_ports = [p for p in pmd2_ports if p in wanted]
        adafruit_ids = [i for i in adafruit_ids if i in wanted]
        all_found = (set(hailo_bdfs)
                     | {b for b, _ in axelera_pairs}
                     | set(deepx_bdfs)
                     | set(memryx_bdfs)
                     | set(pmd2_ports)
                     | set(adafruit_ids))
        missing = wanted - all_found
        if missing:
            print(f"Warning: requested devices not found: {sorted(missing)}",
                  file=sys.stderr)

    if not (hailo_bdfs or axelera_pairs or deepx_bdfs or memryx_bdfs
            or pmd2_ports or adafruit_ids):
        print("No Hailo, Axelera, DeepX, MemryX, PMD2, or Adafruit devices "
              "detected.", file=sys.stderr)
        return 1

    eff_interval = max(0.001, args.interval)
    history_max = max(1, int(round(args.time_range / eff_interval)))

    probe_factories = {
        "hailo": lambda: [HailoProbe(b, history_max=history_max)
                          for b in hailo_bdfs],
        "axelera": lambda: [AxeleraProbe(b, s, verbose=args.verbose,
                                         history_max=history_max)
                            for (b, s) in axelera_pairs],
        "deepx": lambda: [DeepXProbe(b, verbose=args.verbose,
                                     history_max=history_max)
                          for b in deepx_bdfs],
        "memryx": lambda: [MemryXProbe(b, verbose=args.verbose,
                                       history_max=history_max)
                           for b in memryx_bdfs],
        "elmorlabs": lambda: [PMD2Probe(p, verbose=args.verbose,
                                        history_max=history_max)
                              for p in pmd2_ports],
        "adafruit": lambda: [INA228Probe(
            b, addresses=args.ina228_addresses,
            shunt_res=args.ina228_shunt,
            max_current=args.ina228_max_current,
            verbose=args.verbose,
            history_max=history_max) for b in adafruit_ids],
    }
    probes = []
    for probe_type in order:
        probes.extend(probe_factories[probe_type]())

    if args.active_only:
        suppressed = [p for p in probes if p.device is None]
        probes = [p for p in probes if p.device is not None]
        for p in suppressed:
            print(f"[--active-only] hiding {p.bdf} "
                  f"({p.error or 'no API access'})", file=sys.stderr)
            p.close()
        if not probes:
            print("No active devices remaining after --active-only.",
                  file=sys.stderr)
            return 1
    csv_file = None
    if args.csv:
        try:
            csv_file = open(args.csv, "w", buffering=1)
        except OSError as e:
            print(f"Error opening --csv file {args.csv!r}: {e}",
                  file=sys.stderr)
            return 1

    try:
        if args.once:
            do_snapshot(probes)
            if csv_file is not None:
                CSV_SETTLE_MAX_POLLS = 10
                for _ in range(CSV_SETTLE_MAX_POLLS):
                    for p in probes:
                        try:
                            p.poll()
                        except Exception:
                            pass
                    if all(getattr(p, "metrics_settled", True)
                           for p in probes):
                        break
                    time.sleep(args.interval)
                write_csv_snapshot(csv_file, probes)
            return 0
        tui = TUI(probes, interval=args.interval,
                  time_range=args.time_range,
                  graph_rows=args.graph_rows,
                  power_max=args.power_max,
                  temp_max=args.temp_max,
                  csv_file=csv_file)
        curses.wrapper(tui.run)
    except KeyboardInterrupt:
        pass
    finally:
        for p in probes:
            p.close()
        if csv_file is not None:
            try:
                csv_file.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

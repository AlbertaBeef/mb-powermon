# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`mb-powermon.py` is a single-file curses TUI that monitors edge AI NPUs and external power-measurement devices on a workstation. It is adapted from `../envic_ai_python/ai_nvtop.py` with the AMD host probe removed; only four probe types remain: **hailo**, **axelera**, **elmorlabs** (PMD2), and **adafruit** (INA228 over FT232H).

`csv-to-html-plot.py` is an independent companion script that renders an mb-powermon CSV (the `--csv` side-output) as a self-contained HTML file with Chart.js power+temperature plots. It uses min-max bucket downsampling to keep long captures viewable, and with `-l <hailortcli.log>` overlays parsed avg/max markers on the matching device's active phase. The two scripts share no code — the only contract is the CSV column-name convention `<device>_POW / _TEMP / _TS0 / _TS1`.

There is no build, lint, or test suite — the tools are run directly.

## Common commands

```bash
# Live TUI (q/ESC quit, r reset, h help)
python3 mb-powermon.py

# One-shot text snapshot (good for piping/CI)
python3 mb-powermon.py --once

# TUI + line-buffered CSV side-output
python3 mb-powermon.py --csv run.csv

# Restrict probes (also controls top-to-bottom panel order)
python3 mb-powermon.py --probe adafruit,hailo

# Probe-init diagnostics to stderr (which SDK loaded, why temps are missing, etc.)
python3 mb-powermon.py --verbose

# Quick syntax check after edits (no test suite exists)
python3 -c "import ast; ast.parse(open('mb-powermon.py').read())"
```

`--device <ID>` filters to specific BDFs / serial port paths. `--ina228-shunt`, `--ina228-max-current`, `--ina228-addresses` tune the INA228 calibration. `--temp-max` / `--power-max` set the y-axis caps.

## Architecture

The whole program is one file. The interesting design is the **probe → Metric → TUI** decoupling — keep this contract intact when editing.

### The `Metric` contract

Every probe owns a `metrics` list of `Metric(label, history, unit, color_id, max_val)` namedtuples. `history` is a `deque` of `float | None` samples (newest at the right; `None` means "no reading"). The TUI never knows about specific probe internals — it iterates `probe.metrics` to draw inline bars, graph traces, the legend, and the CSV columns. **To add a new measurement, append a `Metric` to the probe's list and push samples in `poll()` — no TUI changes needed.**

`max_val=None` falls back to the unit-based default (`TEMP_MAX` for °C, `POWER_MAX` for W). Set it explicitly when one metric needs a different scale from its peers (e.g. PMD2's `TOTAL` on a 100 W axis while `PCIE1/2/3` share 10 W).

The CP_* color-pair IDs are defined at module scope so probes can reference them during `__init__` — that runs **before** `curses.start_color()`, so don't move them into `_init_colors()`.

### Probe interface

Every probe class implements:

- `__init__(self, ..., history_max=None)` — discover, open, populate `self.metrics`
- `bdf` (str) — display ID; PCI BDF for hailo/axelera, serial path for PMD2, `"adafruit-ft232h"` for INA228
- `pcie` (dict) — `pcie_info(bdf)` result, or `{}` for non-PCI probes
- `identity` (dict) — board_name / arch / product_name / description / part_number / serial
- `device` — truthy iff the SDK handle opened; `None` triggers the red `ERROR` row
- `error` (str | None) — short reason shown next to `ERROR`
- `poll()` — append exactly one sample (or `None`) to every metric's history
- `reset_history()` — clear all deques (bound to the `r` key)
- `close()` — release SDK / serial / collector state cleanly
- Optional `metrics_settled` (bool) — defers CSV header until labels finalize (INA228 uses this to wait for the rail-voltage classification that turns `P1` into `P1(3.3V)`)

### Per-probe quirks worth knowing

- **Hailo** — `get_chip_temperature()` returns both `ts0_temperature` and `ts1_temperature`; we expose them as separate `TS0`/`TS1` metrics. Power is firmware-side averaged via `start_power_measurement()`. Another HailoRT client (e.g. `hailortcli benchmark`) clobbers the averaging buffer; the probe auto-recovers after `_power_restart_threshold` consecutive `None` reads. HailoRT's C library logs an overcurrent-protection warning to stderr on every `start_power_measurement` — `_silence_fd_output()` redirects fds 1/2 around the call so it doesn't corrupt curses.
- **Axelera** — Metis M.2 doesn't expose power, so the `POW` history stays empty by design. Temperatures come from `triton_trace --slog --peek` (the binary is auto-located on `$PATH` or `/opt/axelera/runtime-*/bin`). The collector log line is `core_temps=[a,b,c,d,e]` — **5 values**, not 4 despite the name: index 0 is the Sys-core (module/system PVT monitor), indices 1–4 are AI-cores 0–3. We surface them as separate `SYS` / `AI0` / `AI1` / `AI2` / `AI3` metrics. There's also a `temp_sensor@40` board sensor referenced in Axelera's CONNECT docs (I²C addr 0x40 on the M.2's internal bus), but at firmware 1.5.3-1 it isn't exposed through any host interface — see the `mb-axelera` skill for the full investigation. The probe enables `--slog-level inf:collector` at startup and restores `err` on `close()`; if you forget to close the probe, verbose collector logs leak.
- **PMD2** — 10 rails are polled and stored in `last_snapshot` (used by `--once`), but only `PCIE1/2/3` + `TOTAL` are graphed to keep the panel readable. The PMD2's STM32 internal `Tchip` is the MCU's own temp, not the system's, and is deliberately suppressed.
- **INA228** — `BLINKA_FT232H=1` MUST be set before any Blinka-touching module is imported, because `digitalio` caches the platform decision on first import. The module sets it via `os.environ.setdefault` at the very top — do not import `board`, `digitalio`, or `adafruit_ina228` above that line. Up to four INA228s share one FT232H I2C bus at canonical addresses `{0x40, 0x41, 0x44, 0x45}`. After 3 valid voltage samples per sensor, the metric label gets a sticky rail tag (`P1` → `P1(3.3V)`); CSV header writes are deferred until this finalizes.
- **PCIe ASPM** — needs to read past byte 64 of `/sys/bus/pci/devices/<BDF>/config`, which requires root. `_read_pci_config()` falls back to `sudo -n cat` for passwordless-sudo accounts; otherwise the field shows `<needs root or sudo -n>`.

### CLI / orchestration

`main()` resolves probes in two passes: scan-by-type (`scan_hailo_devices`, `scan_axelera_devices`, `scan_pmd2_devices`, `scan_adafruit_devices`), then instantiate via `probe_factories[type]()` in the order given by `--probe` (or `DEFAULT_PROBE_ORDER = ["adafruit", "hailo", "axelera", "elmorlabs"]`). The order is also the on-screen panel order — `adafruit` leads by default so external/ground-truth power readings sit above chip self-reports.

`--once` calls `do_snapshot()` (text) and optionally `write_csv_snapshot()`. The TUI path opens the CSV in line-buffered mode (so `tail -f` works) and emits one row per `--interval` via `TUI._csv_emit()`.

## Editing notes

- When adding a new probe type, register it in: the `_probe_list` validator, `DEFAULT_PROBE_ORDER`, `probe_factories`, the scan-results aggregation in `main()`, the help-overlay text, and `--device` filtering. Missing one of these is the typical bug.
- The git working tree is on a filesystem with mismatched UID ownership — `git` commands fail with "dubious ownership" until you run `git config --global --add safe.directory <path>`.

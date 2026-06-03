# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`mb-powermon.py` is a single-file curses TUI that monitors edge AI NPUs and external power-measurement devices on a workstation. It is adapted from `../envic_ai_python/ai_nvtop.py` with the AMD host probe removed; six probe types are supported: **hailo**, **axelera**, **deepx**, **memryx**, **elmorlabs** (PMD2), and **adafruit** (INA228 over FT232H).

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

`max_val=None` falls back to the TUI-level default (`TEMP_MAX` for °C, `POWER_MAX` for W — both driven by the `--temp-max` / `--power-max` CLI flags). Set `max_val` explicitly **only** when one metric needs a legitimately different scale from its peers (e.g. PMD2's `TOTAL` on a 100 W axis while `PCIE1/2/3` follow `--power-max`).

**Contract: probe-class `POWER_MAX` is intentionally NOT honored** by the TUI's `_metric_max()`. Earlier versions consulted `getattr(probe, "POWER_MAX", self.POWER_MAX)` so a probe class could "default" its own cap — but this had the surprising side-effect that probe-class defaults shadowed the user's `--power-max` CLI flag. Reverted 2026-05-21: `--power-max` is now authoritative for any "W" metric whose `max_val` is None. If a probe genuinely needs a different scale, use per-metric `max_val` (see PMD2's TOTAL). Don't add `POWER_MAX = N` class attributes — they're dead code and the lookup path that used them is gone. Same applies to setting `max_val=self.POWER_MAX` in metric construction: this would put a probe's notion of "default cap" at the top of the priority chain (above the CLI flag) — don't do it. INA228Probe historically did both; both have been removed.

The CP_* color-pair IDs are defined at module scope so probes can reference them during `__init__` — that runs **before** `curses.start_color()`, so don't move them into `_init_colors()`.

### The `state_flags` contract

Sister contract to `metrics`, for latching status indicators (thermal-throttle, power-alert, etc.). A probe declares `self.state_flags = [(label, deque), ...]` where each deque holds the most recent integer enum value per poll (`0` = OK, non-zero = ALERT, `None` = no reading).

**TUI rendering** — one left-aligned row sitting **between the stats row and the inline-bars row** (so the module-level latches read as part of the panel header, not buried between the bars and the graph), in the form `<PREFIX_LABEL> <prefix_value>  <FLAG_LABEL> [<state> [<state> ...]]  ...`:
- Each LABEL (`CLOCK`, `THERMAL`, `POWER`, ...) is bold-cyan (`CP_ACCENT`), matching the `PRODUCT / DESCRIPTION` style on the stats row above.
- The optional prefix value (e.g. `850MHz`) is dim (`CP_DIM`).
- Brackets `[` `]` are dim — pure scaffolding so the state badge inside pops out.
- Each state badge inside the brackets is bold: green ` OK ` when val == 0, red `ALERT` when val != 0, dim ` -- ` when val is None. ` OK ` and ` -- ` keep the inner spaces intentionally so `[ OK ]` doesn't look cramped next to `[ALERT]`.
- **Group-by-prefix**: consecutive `state_flags` entries whose labels match `PREFIX_<digit>` (e.g. `THERMAL_0`, `THERMAL_1`, ...) collapse into one widget `PREFIX [ s0  s1  ... ]`, each inner badge colored from its own deque. Non-grouped labels render alone as `LABEL [<state>]`. CSV always emits one column per per-chip flag — the grouping is purely a TUI cosmetic.
- Labels should be short, ALL-CAPS keywords — `THERMAL`, `POWER`, `LINK`. Avoid `_ALERT` suffixes; the colour and `[ALERT]` badge already convey state.

**Optional `state_prefix`** — `None` (default), a tuple `(label, value_str)`, or a zero-arg callable returning either. A bare string is also accepted and renders as `STATUS <value>` (fallback for ad-hoc probes that just want one number up front). MemryX returns `("CLOCK", "850MHz")` so the row reads `CLOCK 850MHz  THERMAL [ OK  OK  OK  OK ]  POWER [ OK ]` — the operator sees which boost mode is active right next to the per-chip thermal latches (`THERMAL [ OK  OK ALERT  OK ]` is much more interesting paired with a known 850 MHz boost than at the 600 MHz baseline because it pinpoints both the dropped clock and the tripped die). The prefix is display-only — it's not logged to CSV (matches the user's "CLK is not a metric" decision).

**Layout cost** — `_draw_device()` reserves one extra row between the stats row and the inline-bars row when `state_flags` is non-empty (pushing the bars + graph down by one), then `_draw_state_flags()` does the rendering. Both `_csv_emit()` (TUI path) and `write_csv_snapshot()` (`--once --csv` path) iterate `state_flags` to emit one `<bdf>_<label>` column per flag, and `do_snapshot()` prints the current state per flag in the text snapshot.

Probes with no enum status leave `self.state_flags = []` (or omit the attribute entirely — the TUI uses `getattr(probe, "state_flags", None) or []`). Today only `MemryXProbe` populates it: the per-chip `THERMAL_<i>` entries are always present when `/sys/memx<N>/temperature` is readable (no SDK dependency), and the module-level `POWER` entry appears only when the MX3 SDK + power capability are both available.

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
- Optional `state_flags` (list) — non-numeric enum indicators (`[(label, deque), ...]`); see "The `state_flags` contract" above
- Optional `state_prefix` (`None` / `(label, value_str)` tuple / callable / bare str) — leading `LABEL value` chunk on the status row, before the flag badges; see same section

### Per-probe quirks worth knowing

- **Hailo** — `get_chip_temperature()` returns both `ts0_temperature` and `ts1_temperature`; we expose them as separate `TS0`/`TS1` metrics. Power is firmware-side averaged via `start_power_measurement()`. Another HailoRT client (e.g. `hailortcli benchmark`) clobbers the averaging buffer; the probe auto-recovers after `_power_restart_threshold` consecutive `None` reads. HailoRT's C library logs an overcurrent-protection warning to stderr on every `start_power_measurement` — `_silence_fd_output()` redirects fds 1/2 around the call so it doesn't corrupt curses. `HailoProbe(bdf, sdk_device=<Device>)` also supports a **borrow mode**: when an embedding caller (e.g. an external project running an HailoInference VDevice) passes a pre-opened Device, the probe uses that handle instead of opening its own — eliminating the firmware-buffer contention by construction. Borrow mode is the symmetric counterpart to `AxeleraProbe(sdk_device=...)`; standalone mb-powermon never uses it.
- **Axelera** — Metis M.2 doesn't expose power, so the `POW` history stays empty by design. Temperatures come from `triton_trace --slog --peek` (the binary is auto-located on `$PATH` or `/opt/axelera/runtime-*/bin`). The collector log line is `core_temps=[a,b,c,d,e]` — **5 values**, not 4 despite the name: index 0 is the Sys-core (module/system PVT monitor), indices 1–4 are AI-cores 0–3. We surface them as separate `SYS` / `AI0` / `AI1` / `AI2` / `AI3` metrics. There's also a `temp_sensor@40` board sensor referenced in Axelera's CONNECT docs (I²C addr 0x40 on the M.2's internal bus), but at firmware 1.5.3-1 it isn't exposed through any host interface — see the `mb-axelera` skill for the full investigation. The probe enables `--slog-level inf:collector` at startup and restores `err` on `close()`; if you forget to close the probe, verbose collector logs leak.
- **DeepX** — DX_M1 (PCI vendor `0x1ff4`) doesn't expose power, so the `POW` history stays empty by design. Temperatures come from shelling out to `dxrt-cli -s` (auto-located on `$PATH` or `/usr/local/bin/dxrt-cli`); the `dx_engine` Python SDK is for inference only, not telemetry. The CLI's status block contains one line per NPU core — `NPU 0: voltage 750 mV, clock 1000 MHz, temperature 29'C` — and we parse temperature into `T0`/`T1`/`T2` (M1 has 3 NPU cores). Voltage and clock are static constants on this device, so they're not graphed. Identity (firmware version, RT/PCIe driver versions, memory size) is parsed once from `dxrt-cli -i` at `_open()`.
- **MemryX** — MX3 (driver `memx_pcie_ai_chip`, DKMS module `memx_cascade_plus_pcie`). Three independent telemetry sources, each gracefully degrading on its own:
   - **Per-MPU temperatures (hwmon, always available)** — driver registers `/sys/class/hwmon/hwmonN/` with `name="memx0"` and 16 `tempN_input` files; only the populated slots return values. On the 4-chip MX3-2280-M-4 SKU, `temp1..temp4` populate one per MPU (matching the MPU 0 → MPU 3 dataflow pipeline); on a 2-chip SKU only `temp1..temp2`. The probe autodetects how many MPUs are present and emits a corresponding `T0..T(N-1)` metric per chip. Scanning is hwmon-name-based (`scan_sysfs_memryx()` walks `/sys/class/hwmon` for `name="memx0"` and resolves the BDF via the `device` symlink) — no PCI vendor ID match needed, robust to MemryX's vendor ID not being in the public PCI registry.
   - **Per-chip thermal throttle state (vendor sysfs, always available)** — parsed from `/sys/memx<N>/temperature` via `MEMRYX_SYSFS_RE` (one line per chip in the form `CHIP(i) PVT3 Temperature: ... (ThermalThrottlingState: <0|1>)`; the PVT sensor index varies — firmware picks whichever on-die sensor reads hottest at read time — and the regex skips it with `.*?` between `CHIP(N)` and `ThermalThrottlingState`). This is the **only** per-chip throttle source — `mxa.get_thermal_state(device_id)` returns a single module-level enum (no `chip_id` arg; the firmware effectively ORs the per-MPU latches). Exposed as `state_flags = [("THERMAL_0", ...), ("THERMAL_1", ...), ..., ("THERMAL_{N-1}", ...)]`. The TUI groups these by prefix into one `THERMAL [ s0  s1  s2  s3 ]` widget; CSV emits one column per chip.
   - **SDK telemetry (`memryx.mxa`, optional)** — when the MX3 SDK is installed and `mxa.get_power(device_id)` returns a numeric reading: `POW` (mW → W, graphed on the standard `--power-max` axis), voltage (mV) and clock (MHz) into `last_snapshot` (operating-point info, not graphed), and module-level `POWER` state-flag from `mxa.get_poweralert()`. The probe also exposes `state_prefix = ("CLOCK", "<freq>MHz")` so the status row reads `CLOCK 850MHz  THERMAL [ OK  OK  OK  OK ]  POWER [ OK ]`. If the SDK is absent or capability-gated off, the `POW` deque stays `None`, `POWER` flag and `CLOCK` prefix are omitted, and the row collapses to just the per-chip THERMAL widget.
   - **`identity["arch"]` quirk**: avoid periods. The snapshot formatter does `str(arch).split(".")[-1]` to unwrap enum qualnames like `BoardType.METIS`, which would also strip `"M.2"` to `"2"`. MemryXProbe uses `f"M2 ({n}-chip)"`.
- **PMD2** — 10 rails are polled and stored in `last_snapshot` (used by `--once`), but only `PCIE1/2/3` + `TOTAL` are graphed to keep the panel readable. The PMD2's STM32 internal `Tchip` is the MCU's own temp, not the system's, and is deliberately suppressed.
- **INA228** — `BLINKA_FT232H=1` MUST be set before any Blinka-touching module is imported, because `digitalio` caches the platform decision on first import. The module sets it via `os.environ.setdefault` at the very top — do not import `board`, `digitalio`, or `adafruit_ina228` above that line. Up to four INA228s share one FT232H I2C bus at canonical addresses `{0x40, 0x41, 0x44, 0x45}`. After 3 valid voltage samples per sensor, the metric label gets a sticky rail tag (`P1` → `P1(3.3V)`); CSV header writes are deferred until this finalizes.
- **PCIe ASPM** — needs to read past byte 64 of `/sys/bus/pci/devices/<BDF>/config`, which is gated by `CAP_SYS_ADMIN` (unprivileged readers see a 64-byte truncation, not a permission error — the file is `0644` but the kernel refuses the read internally). `_read_pci_config()` falls back to `sudo -n cat` for passwordless-sudo accounts; otherwise the field shows `<needs root or sudo -n>`. The recommended workaround is a narrow sudoers entry that grants passwordless read on PCI config space only — `<user> ALL=(root) NOPASSWD: /usr/bin/cat /sys/bus/pci/devices/*/config` in `/etc/sudoers.d/mb-powermon-aspm` (mode `0440`). **Don't run the whole tool under `sudo`** to fix ASPM: `sudo python3` uses the system Python, which doesn't see the venv that holds the vendor SDKs (`hailo_platform`, `memryx`, `adafruit_ina228`, ...) — every SDK-dependent probe silently downgrades to ERROR. If a one-off root run is genuinely needed, invoke the venv's interpreter explicitly (`sudo /path/to/venv/bin/python3 mb-powermon.py`); for normal use, install the sudoers entry and run as your normal user.

### CLI / orchestration

`main()` resolves probes in two passes: scan-by-type (`scan_hailo_devices`, `scan_axelera_devices`, `scan_deepx_devices`, `scan_memryx_devices`, `scan_pmd2_devices`, `scan_adafruit_devices`), then instantiate via `probe_factories[type]()` in the order given by `--probe` (or `DEFAULT_PROBE_ORDER = ["adafruit", "hailo", "axelera", "deepx", "memryx", "elmorlabs"]`). The order is also the on-screen panel order — `adafruit` leads by default so external/ground-truth power readings sit above chip self-reports.

`--once` calls `do_snapshot()` (text) and optionally `write_csv_snapshot()`. The TUI path opens the CSV in line-buffered mode (so `tail -f` works) and emits one row per `--interval` via `TUI._csv_emit()`.

## Editing notes

- When adding a new probe type, register it in: the `_probe_list` validator, `DEFAULT_PROBE_ORDER`, `probe_factories`, the scan-results aggregation in `main()`, the help-overlay text, and `--device` filtering. Missing one of these is the typical bug.
- The git working tree is on a filesystem with mismatched UID ownership — `git` commands fail with "dubious ownership" until you run `git config --global --add safe.directory <path>`.

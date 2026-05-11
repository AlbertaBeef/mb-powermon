#!/usr/bin/env python3
"""
mb-powermon-plot.py — offline HTML plot generator for mb-powermon CSV output.

Generates a self-contained HTML file with Chart.js plots of power and
temperature data captured by mb-powermon.py. Optionally overlays
hailortcli-reported averages and maxima from a corresponding log file.

Usage:
    python3 mb-powermon-plot.py -i data.csv [-l benchmark.log] [-o out.html]

Recognized CSV columns:
    time                          — ISO-format timestamp (required, first col)
    <device>_POW                  — power reading (W)
    <device>_TEMP                 — single temperature reading (°C)
    <device>_TS0, _TS1            — Hailo on-die thermal sensors (°C)
    <device>_T0, _T1, _T2, ...    — DeepX per-NPU temperatures (°C)
    <device>_SYS                  — Axelera module/system PVT (°C)
    <device>_AI0, _AI1, ...       — Axelera per-AIPU-core PVT (°C)
    <device>_PCIE1, _PCIE2, ...   — PMD2 per-rail power (W)
    <device>_TOTAL                — PMD2 aggregate power (W)
    adafruit-ft232h_*             — Adafruit FT232H power channels (W)

Anything else is silently skipped (with a stderr warning).
"""

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

# Known devices get curated colors. Add more entries here to extend.
DEVICE_COLORS = {
    "0000:c6:00.0": {"pow": "#1D9E75", "ts0": "#1D9E75",
                    "ts1": "#6BC09A", "temp": "#1D9E75"},
    "0000:c4:00.0": {"pow": "#D85A30", "ts0": "#D85A30",
                    "ts1": "#ED9472", "temp": "#D85A30"},
}

# Fallback palette for any device not listed above.
FALLBACK_PALETTE = ["#534AB7", "#3478C2", "#A53860",
                    "#5C8001", "#8E44AD", "#D4A017"]

# PMD2 rail colors — distinct so PCIE1/2/3 don't collapse to a single hue.
RAIL_COLORS = {
    "pcie1": "#534AB7",
    "pcie2": "#3478C2",
    "pcie3": "#A53860",
}

LOG_OVERLAY_COLOR = "#BA7517"


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

# Matches column names like "0000:c6:00.0_POW", "device_TS0", "device_T2"
# (DeepX per-NPU), "device_SYS" / "device_AI3" (Axelera per-core), or
# "device_TOTAL" (PMD2 aggregate power).
_METRIC_RE = re.compile(
    r"^(.+?)_(POW|TEMP|TS\d+|T\d+|SYS|AI\d+|PCIE\d+|TOTAL)$"
)


def _classify_metric(met):
    """Return 'power' or 'temp' for a metric kind, or None to skip.

    Power-like (plotted on the power chart):
        pow, total, pcieN (PMD2 per-rail), and adafruit-ft232h_PN power
        channels handled in parse_header().
    Temperature-like (plotted on the temperature chart):
        temp, sys (Axelera module sensor), tsN (Hailo on-die), tN (DeepX
        per-NPU), aiN (Axelera per-core).
    """
    if met in ("pow", "total"):
        return "power"
    if re.match(r"^pcie\d+$", met):
        return "power"
    if met in ("temp", "sys"):
        return "temp"
    if re.match(r"^(ts|t|ai)\d+$", met):
        return "temp"
    return None


def parse_header(header):
    """Parse CSV header into a list of (device, metric, col_idx, raw_name)."""
    columns = []
    for i, col in enumerate(header):
        if col == "time" or not col.strip():
            continue
        m = _METRIC_RE.match(col)
        if m:
            columns.append((m.group(1), m.group(2).lower(), i, col))
        elif col.startswith("adafruit"):
            # Adafruit FT232H channels have implicit power semantics
            columns.append((col, "pow", i, col))
        else:
            print(f"warning: unrecognized column '{col}', skipping",
                  file=sys.stderr)
    return columns


def read_csv(path):
    """Read CSV; return (header_list, list_of_rows)."""
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [r for r in reader if r and r[0]]
    return header, rows


def parse_timestamp(s):
    """Parse ISO-format timestamp; tolerate trailing comma artifacts."""
    return datetime.fromisoformat(s.rstrip(","))


def safe_float(s):
    if s is None or s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Min-max downsampling
# ---------------------------------------------------------------------------

def auto_bucket_size(n_rows, target=600):
    """Pick a bucket size so output lands near `target` points (after 2x for min+max)."""
    if n_rows <= target:
        return 1
    return max(1, n_rows * 2 // target)


def minmax_downsample(rows, col_idx, bucket_size):
    """
    Bucket downsample preserving both peaks and troughs.
    Each bucket contributes (at most) two output points: its min and its max,
    in their original chronological order. Buckets with no valid data are
    dropped entirely.
    """
    if bucket_size <= 1:
        out = []
        for r in rows:
            v = safe_float(r[col_idx]) if col_idx < len(r) else None
            out.append((r[0], v))
        return out

    out = []
    for i in range(0, len(rows), bucket_size):
        bucket = rows[i:i + bucket_size]
        candidates = []
        for j, r in enumerate(bucket):
            if col_idx >= len(r):
                continue
            v = safe_float(r[col_idx])
            if v is not None:
                candidates.append((j, r[0], v))
        if not candidates:
            continue
        if len(candidates) == 1:
            out.append((candidates[0][1], candidates[0][2]))
            continue
        lo = min(candidates, key=lambda c: c[2])
        hi = max(candidates, key=lambda c: c[2])
        if lo[0] == hi[0]:
            out.append((lo[1], lo[2]))
        else:
            chrono = sorted([lo, hi], key=lambda c: c[0])
            out.append((chrono[0][1], chrono[0][2]))
            out.append((chrono[1][1], chrono[1][2]))
    return out


def build_series(rows, col_idx, t0, bucket_size):
    """Build a [{x: t_seconds, y: value}, ...] series suitable for Chart.js."""
    samples = minmax_downsample(rows, col_idx, bucket_size)
    series = []
    for t_str, v in samples:
        try:
            t_sec = (parse_timestamp(t_str) - t0).total_seconds()
        except ValueError:
            continue
        series.append({"x": round(t_sec, 3), "y": v})
    return series


# ---------------------------------------------------------------------------
# Active-phase detection (used to place log overlays)
# ---------------------------------------------------------------------------

def detect_active_phases(rows, col_idx, t0, threshold=1.0):
    """Find continuous regions where column value exceeds threshold.

    Default threshold is 1.0 W: above Hailo idle (~0.55 W) but below the
    HW-latency phase's bursty floor (~1.2 W), so all three hailortcli
    sub-phases are captured when present. Override via --phase-threshold
    if your accelerator has a different idle/active separation.
    """
    phases = []
    in_phase = False
    start_t = None
    last_t = None
    for r in rows:
        try:
            t = (parse_timestamp(r[0]) - t0).total_seconds()
        except (ValueError, IndexError):
            continue
        v = safe_float(r[col_idx]) if col_idx < len(r) else None
        active = v is not None and v > threshold
        if active and not in_phase:
            start_t = t
            in_phase = True
        elif not active and in_phase:
            phases.append((start_t, last_t))
            in_phase = False
        if active:
            last_t = t
    if in_phase and last_t is not None:
        phases.append((start_t, last_t))
    return phases


# ---------------------------------------------------------------------------
# Hailo log parsing
# ---------------------------------------------------------------------------

_LOG_SUMMARY_RE = re.compile(
    r"Device\s+(\S+?):\s*\n"
    r"\s*Power in streaming mode\s*\(average\)\s*=\s*([\d.]+)\s*W\s*\n"
    r"\s*\(max\)\s*=\s*([\d.]+)\s*W",
    re.MULTILINE,
)


def parse_log(path):
    """
    Extract per-device streaming-power summaries from a hailortcli log.
    Returns {device_id: [{'avg': float, 'max': float}, ...]} in order of appearance.
    """
    summaries = {}
    if not path:
        return summaries
    text = Path(path).read_text()
    for m in _LOG_SUMMARY_RE.finditer(text):
        device, avg, mx = m.group(1), float(m.group(2)), float(m.group(3))
        summaries.setdefault(device, []).append({"avg": avg, "max": mx})
    return summaries


def _phase_mean_power(rows, col_idx, t0, t_start, t_end):
    """Mean of valid power samples within [t_start, t_end] (seconds since t0)."""
    total = 0.0
    n = 0
    for r in rows:
        if not r:
            continue
        try:
            t = (parse_timestamp(r[0]) - t0).total_seconds()
        except (ValueError, IndexError):
            continue
        if t < t_start or t > t_end:
            continue
        v = safe_float(r[col_idx]) if col_idx < len(r) else None
        if v is not None:
            total += v
            n += 1
    return total / n if n else None


def _pick_streaming_phase(rows, col, t0, cluster, log_entry, subphase_dur):
    """Choose the streaming sub-phase out of an invocation cluster.

    `hailortcli benchmark` runs three sub-phases in fixed order — HW-only,
    streaming, HW-latency. They typically appear in the CSV as 3 distinct
    active phases, but two non-canonical cases come up often:

      - **HW-only + streaming merged** into one long phase. Hailo doesn't
        power-cycle the chip across that firmware mode change, so unless
        a sample happens to land in the brief inter-mode dip the two
        sustained-busy cycles fuse into one ~30 s phase. Streaming is
        the second half.
      - **HW-latency below threshold** (Hailo's HW-latency draws ~1.2 W,
        very close to the active-phase threshold). When that's the case
        we either see 2 phases (HW-only, streaming) or 1 phase (those two
        merged); HW-latency just doesn't show up.

    The picker dispatches on cluster size:

      - 3 phases: middle one (canonical).
      - 2 phases:
          * If their mean powers differ by >2× → one is bursty HW-latency,
            the other is HW-only+streaming merged; pick second half of
            the merged phase.
          * Else both are sustained-busy → HW-only and streaming (HW-
            latency missed); streaming is the *later* one (hailortcli's
            fixed run order).
      - 1 phase:
          * Duration > 1.5 × `subphase_dur` → HW-only+streaming merged;
            return the second half.
          * Otherwise return as-is (unusual single-sub-phase capture).
      - 4+ phases: pick the one whose mean is closest to the log's avg.
    """
    n = len(cluster)
    if n == 3:
        return cluster[1]

    if n == 2:
        a, b = cluster
        ma = _phase_mean_power(rows, col, t0, a[0], a[1])
        mb = _phase_mean_power(rows, col, t0, b[0], b[1])
        if ma is not None and mb is not None and min(ma, mb) > 0:
            ratio = max(ma, mb) / min(ma, mb)
            if ratio > 2.0:
                # One bursty (HW-latency), one busy (HW-only+streaming
                # merged). Streaming is the second half of the busy one.
                merged = a if ma > mb else b
                m_dur = merged[1] - merged[0]
                return (merged[0] + m_dur / 2.0, merged[1])
        # Both sustained-busy: HW-only first, streaming second.
        return b

    if n == 1:
        a = cluster[0]
        dur = a[1] - a[0]
        if dur > 1.5 * subphase_dur:
            # Merged HW-only + streaming; streaming is the second half.
            return (a[0] + dur / 2.0, a[1])
        return a

    # 4+ phases: fall back to mean-magnitude match against log's avg.
    target = log_entry["avg"]
    best = None
    best_err = float("inf")
    for sub_ts, sub_te in cluster:
        m = _phase_mean_power(rows, col, t0, sub_ts, sub_te)
        if m is None:
            continue
        e = abs(m - target)
        if e < best_err:
            best_err = e
            best = (sub_ts, sub_te)
    return best if best is not None else cluster[len(cluster) // 2]


def assign_log_overlays(log_summaries, rows, columns, t0,
                        cluster_gap=10.0, subphase_dur=15.0,
                        threshold=None):
    """
    Place each log entry on the streaming sub-phase of its invocation.

    Procedure:
      1) Detect active power phases per device (above `threshold`, or the
         function's own default if None).
      2) Group temporally-close phases into invocation clusters
         (sub-phases sit ~1-3 s apart; invocations sit much further apart,
         controlled by `cluster_gap`).
      3) For each cluster, pick the streaming sub-phase via
         `_pick_streaming_phase` (handles 1/2/3/4+ phase shapes).
    """
    overlays = []
    power_cols = {dev: idx for dev, met, idx, _ in columns if met == "pow"}

    for device, entries in log_summaries.items():
        if device not in power_cols:
            print(f"warning: log mentions {device} but no _POW column found in CSV",
                  file=sys.stderr)
            continue
        col = power_cols[device]
        if threshold is None:
            phases = detect_active_phases(rows, col, t0)
        else:
            phases = detect_active_phases(rows, col, t0, threshold=threshold)
        if not phases:
            print(f"warning: {device} has {len(entries)} log entr(y/ies) "
                  f"but no active phases detected; skipping",
                  file=sys.stderr)
            continue

        # Cluster phases by temporal gap to recover invocation boundaries.
        clusters = [[phases[0]]]
        for ts, te in phases[1:]:
            if ts - clusters[-1][-1][1] < cluster_gap:
                clusters[-1].append((ts, te))
            else:
                clusters.append([(ts, te)])

        if len(clusters) < len(entries):
            print(f"warning: {device} has {len(entries)} log entr(y/ies) "
                  f"but only {len(clusters)} invocation cluster(s); some "
                  f"markers may be misplaced — try lowering --invocation-gap",
                  file=sys.stderr)
        elif len(clusters) > len(entries):
            print(f"info: {device} detected {len(clusters)} cluster(s) for "
                  f"{len(entries)} log entr(y/ies); using the first "
                  f"{len(entries)} in chronological order", file=sys.stderr)

        for entry, cluster in zip(entries, clusters):
            ts, te = _pick_streaming_phase(rows, col, t0, cluster,
                                           entry, subphase_dur)
            overlays.append({"device": device, "t_start": ts, "t_end": te,
                             "avg": entry["avg"], "max": entry["max"]})
    return overlays


# ---------------------------------------------------------------------------
# Dataset construction
# ---------------------------------------------------------------------------

def get_color(device, kind):
    # Exact (device, kind) override — curated palette entries.
    if device in DEVICE_COLORS and kind in DEVICE_COLORS[device]:
        return DEVICE_COLORS[device][kind]
    # Power-rail conventions (PMD2 PCIE1/2/3 etc.)
    if kind in RAIL_COLORS:
        return RAIL_COLORS[kind]
    # Fallback — hash on BOTH device and kind so a multi-sensor probe
    # (DeepX T0/T1/T2, Axelera SYS+AI0..AI3) gets distinct colors per
    # sensor even when the device isn't in DEVICE_COLORS. Hashing on
    # device alone would collapse all sensors of one chip to one hue.
    return FALLBACK_PALETTE[abs(hash((device, kind))) % len(FALLBACK_PALETTE)]


def is_pci_device(name):
    return bool(re.match(r"^[0-9a-f]{4}:[0-9a-f]{2}", name))


def build_datasets(rows, columns, overlays, t0, bucket_size):
    power, temp = [], []

    for dev, met, idx, raw_name in columns:
        series = build_series(rows, idx, t0, bucket_size)
        color = get_color(dev, met)
        kind = _classify_metric(met)
        if kind == "power":
            dash = [6, 4] if is_pci_device(dev) else [2, 3] if not dev.startswith("adafruit") else []
            power.append({
                "label": raw_name, "data": series,
                "borderColor": color, "backgroundColor": color,
                "borderDash": dash, "borderWidth": 1.5,
                "pointRadius": 0, "pointHoverRadius": 4,
                "tension": 0.1, "spanGaps": True,
            })
        elif kind == "temp":
            # Dash the "extra" sensor on multi-sensor probes so it's
            # easy to disambiguate from the canonical one:
            #   Hailo  TS1   — secondary of two thermal sensors
            #   Axelera SYS  — module/system PVT, distinct from AI-cores
            dash = [6, 4] if met in ("ts1", "sys") else []
            temp.append({
                "label": raw_name, "data": series,
                "borderColor": color, "backgroundColor": color,
                "borderDash": dash, "borderWidth": 1.5,
                "pointRadius": 0, "pointHoverRadius": 4,
                "tension": 0.2, "spanGaps": True,
            })

    # Hailo log overlays as horizontal markers
    for ov in overlays:
        power.append({
            "label": f"{ov['device']} log avg = {ov['avg']:.3f} W",
            "data": [{"x": ov["t_start"], "y": ov["avg"]},
                     {"x": ov["t_end"],   "y": ov["avg"]}],
            "borderColor": LOG_OVERLAY_COLOR, "backgroundColor": LOG_OVERLAY_COLOR,
            "borderWidth": 3, "pointRadius": 3, "pointStyle": "rectRot",
            "tension": 0, "showLine": True,
        })
        if abs(ov["max"] - ov["avg"]) > 1e-4:
            power.append({
                "label": f"{ov['device']} log max = {ov['max']:.3f} W",
                "data": [{"x": ov["t_start"], "y": ov["max"]},
                         {"x": ov["t_end"],   "y": ov["max"]}],
                "borderColor": LOG_OVERLAY_COLOR, "borderDash": [4, 3],
                "borderWidth": 2, "pointRadius": 0,
                "tension": 0, "showLine": True,
            })
    return power, temp


# ---------------------------------------------------------------------------
# HTML output
# ---------------------------------------------------------------------------

def legend_item_html(ds):
    color = ds["borderColor"]
    if ds.get("borderDash"):
        swatch = (f'<span style="display:inline-block;width:18px;height:0;'
                  f'border-top:2px dashed {color};"></span>')
    else:
        swatch = (f'<span style="display:inline-block;width:18px;height:2px;'
                  f'background:{color};"></span>')
    return f'<span style="display:flex;align-items:center;gap:6px;">{swatch}{ds["label"]}</span>'


def legend_html(label, datasets):
    if not datasets:
        return ""
    items = "".join(legend_item_html(ds) for ds in datasets)
    return ('<div style="display:flex;flex-wrap:wrap;gap:14px;'
            'margin:0.5rem 0;font-size:12px;color:#555;">'
            f'<strong style="color:#222;">{label}:</strong>{items}</div>')


HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
         sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem;
         color: #222; }
  h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
  h2 { font-size: 1rem; color: #555; margin: 1.5rem 0 0.25rem; font-weight: normal; }
  .meta { color: #666; font-size: 0.85rem; margin-bottom: 1.5rem; }
  .chart { position: relative; width: 100%; height: 280px; margin-bottom: 12px; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="meta">Source: __SOURCE__ · Generated: __GENERATED__</div>
"""

CHART_JS = """
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
const DATA = __DATA_JSON__;

function makeChart(canvasId, datasets, yLabel, xMax, isTemp) {
  new Chart(document.getElementById(canvasId), {
    type: 'line',
    data: { datasets: datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => 't = ' + items[0].parsed.x.toFixed(1) + ' s',
            label: (item) => item.dataset.label + ': ' +
              (item.parsed.y == null ? 'n/a' :
               item.parsed.y.toFixed(3) + (isTemp ? ' °C' : ' W'))
          }
        }
      },
      scales: {
        x: {
          type: 'linear',
          title: { display: true, text: 'elapsed time (s)' },
          min: 0, max: xMax
        },
        y: {
          title: { display: true, text: yLabel },
          beginAtZero: !isTemp
        }
      }
    }
  });
}

if (DATA.power_datasets && DATA.power_datasets.length)
  makeChart('powChart', DATA.power_datasets, 'power (W)', DATA.x_max, false);
if (DATA.temp_datasets && DATA.temp_datasets.length)
  makeChart('tempChart', DATA.temp_datasets, 'temperature (°C)', DATA.x_max, true);
</script>
</body>
</html>
"""


def build_html(title, source, power_datasets, temp_datasets, x_max):
    head = (HTML_HEAD
            .replace("__TITLE__", title)
            .replace("__SOURCE__", source)
            .replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M")))

    body = ""
    if power_datasets:
        body += "<h2>Power</h2>" + legend_html("Power", power_datasets)
        body += '<div class="chart"><canvas id="powChart"></canvas></div>'
    if temp_datasets:
        body += "<h2>Temperature</h2>" + legend_html("Temperature", temp_datasets)
        body += '<div class="chart"><canvas id="tempChart"></canvas></div>'

    data = {"x_max": x_max,
            "power_datasets": power_datasets,
            "temp_datasets": temp_datasets}

    js = CHART_JS.replace("__DATA_JSON__", json.dumps(data))
    return head + body + js


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Generate offline HTML plots from mb-powermon CSV output.")
    p.add_argument("-i", "--input", required=True, help="input CSV file")
    p.add_argument("-l", "--log",
                   help="optional hailortcli log file (overlays avg/max markers)")
    p.add_argument("-o", "--output",
                   help="output HTML file (default: <input>.html)")
    p.add_argument("-d", "--downsample", type=int,
                   help="bucket size for min-max downsampling "
                        "(default: auto, 1 = disable)")
    p.add_argument("-t", "--title",
                   help="chart title (default: input filename stem)")
    p.add_argument("--invocation-gap", type=float, default=10.0, metavar="SEC",
                   help="seconds — gaps larger than this between active power "
                        "phases mark a new hailortcli invocation boundary "
                        "(default: 10.0). Lower if your invocations were "
                        "back-to-back; raise if a single invocation has long "
                        "idle dips between its 3 sub-phases.")
    p.add_argument("--phase-threshold", type=float, default=None, metavar="W",
                   help="power level (W) above which the device is considered "
                        "running an active sub-phase (default: 1.0 — above "
                        "Hailo idle ~0.55 W, below HW-latency floor ~1.2 W). "
                        "Lower this if HW-latency isn't being detected as a "
                        "third sub-phase; raise if idle-period noise is being "
                        "misclassified as activity.")
    p.add_argument("--subphase-duration", type=float, default=15.0, metavar="SEC",
                   help="expected duration of a single hailortcli sub-phase "
                        "(default: 15.0, the hailortcli `--time-to-run` "
                        "default). Used to detect when HW-only and streaming "
                        "have merged into one long phase: phases >1.5× this "
                        "are split in half and the marker placed on the "
                        "second half.")
    args = p.parse_args()

    output = args.output or str(Path(args.input).with_suffix(".html"))
    title = args.title or Path(args.input).stem

    header, rows = read_csv(args.input)
    if not rows:
        print(f"error: no data rows in {args.input}", file=sys.stderr)
        sys.exit(1)

    columns = parse_header(header)
    bucket_size = args.downsample if args.downsample is not None \
                  else auto_bucket_size(len(rows))

    t0 = parse_timestamp(rows[0][0])
    t_end = (parse_timestamp(rows[-1][0]) - t0).total_seconds()
    x_max = round(t_end + 1, 1)

    log_summaries = parse_log(args.log) if args.log else {}
    overlays = assign_log_overlays(log_summaries, rows, columns, t0,
                                   cluster_gap=args.invocation_gap,
                                   subphase_dur=args.subphase_duration,
                                   threshold=args.phase_threshold)

    power_ds, temp_ds = build_datasets(rows, columns, overlays, t0, bucket_size)
    html = build_html(title, Path(args.input).name, power_ds, temp_ds, x_max)
    Path(output).write_text(html)

    n_power = len([d for d in power_ds if "log" not in d["label"]])
    n_temp = len(temp_ds)
    print(f"wrote {output}")
    print(f"  source: {args.input} ({len(rows)} rows, downsample={bucket_size})")
    print(f"  series: {n_power} power, {n_temp} temperature, {len(overlays)} log overlays")


if __name__ == "__main__":
    main()

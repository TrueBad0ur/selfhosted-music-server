#!/usr/bin/env python3
import subprocess, json, time, shutil, os, csv
from datetime import datetime, timedelta

INTERVAL      = 10    # seconds between readings
WINDOW_H      = 24    # hours kept in CSV file
DISPLAY_MIN   = 30    # minutes shown in the graph
BUCKET_SEC    = 60    # seconds per column (1 column = 1 minute)
GRAPH_H       = 15    # graph rows (15 rows × 5°C = 75°C range)
MIN_T     = 35.0
MAX_T     = 110.0
DATA_FILE = os.path.expanduser("~/.temp_monitor.csv")

SENSORS = [
    ("CPU  Tctl",  "k10temp-pci-00c3",  "Tctl",      "\033[36m"),  # cyan
    # ("GPU  Edge",  "amdgpu-pci-0700",   "edge",      "\033[32m"),
    # ("GPU  Junc",  "amdgpu-pci-0300",   "junction",  "\033[33m"),
    # ("NVMe Comp",  "nvme-pci-0600",     "Composite", "\033[34m"),
]
LABELS = [s[0] for s in SENSORS]
RESET  = "\033[0m"

# ── persistence ──────────────────────────────────────────────────────────────

def load_history():
    rows = []
    if not os.path.exists(DATA_FILE):
        return rows
    cutoff = datetime.now() - timedelta(hours=WINDOW_H)
    try:
        with open(DATA_FILE, newline="") as f:
            for r in csv.DictReader(f):
                ts = datetime.fromisoformat(r["ts"])
                if ts >= cutoff:
                    rows.append((ts, {l: float(r[l]) if r[l] else None for l in LABELS}))
    except Exception:
        pass
    return rows

def append_row(ts, readings):
    write_header = not os.path.exists(DATA_FILE)
    with open(DATA_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["ts"] + LABELS)
        w.writerow([ts.isoformat()] + [readings.get(l) for l in LABELS])

def prune_file():
    """Rewrite file keeping only last WINDOW_H hours (run periodically)."""
    rows = load_history()
    if not rows:
        return
    with open(DATA_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts"] + LABELS)
        for ts, vals in rows:
            w.writerow([ts.isoformat()] + [vals.get(l) for l in LABELS])

# ── sensors ──────────────────────────────────────────────────────────────────

def read_sensors():
    try:
        out = subprocess.run(["sensors", "-j"], capture_output=True, text=True, timeout=5)
        data = json.loads(out.stdout)
    except Exception:
        return {}
    result = {}
    for label, chip, sensor, _ in SENSORS:
        try:
            vals = data[chip][sensor]
            key  = next(k for k in vals if k.endswith("_input"))
            result[label] = float(vals[key])
        except Exception:
            result[label] = None
    return result

# ── display ──────────────────────────────────────────────────────────────────

def temp_color(val):
    if val is None: return RESET
    if val >= 95:   return "\033[31m"
    if val >= 75:   return "\033[33m"
    return "\033[32m"

def bar(val, width=28):
    if val is None:
        return "─" * width
    frac   = max(0.0, min(1.0, (val - MIN_T) / (MAX_T - MIN_T)))
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)

def bucket(rows, display_minutes, graph_w):
    """Fit display_minutes into graph_w columns, each col = display_minutes*60/graph_w seconds."""
    result    = [{l: None for l in LABELS} for _ in range(graph_w)]
    now       = datetime.now()
    start     = now - timedelta(minutes=display_minutes)
    span_sec  = display_minutes * 60
    for ts, vals in rows:
        if ts < start:
            continue
        idx = int((ts - start).total_seconds() / span_sec * graph_w)
        idx = max(0, min(graph_w - 1, idx))
        for l in LABELS:
            v = vals.get(l)
            if v is not None:
                prev = result[idx][l]
                result[idx][l] = max(prev, v) if prev is not None else v
    return result

def x_axis_labels(display_minutes, graph_w):
    """X-axis labels every 5 minutes."""
    now   = datetime.now()
    start = now - timedelta(minutes=display_minutes)
    marks = {}
    for m in range(0, int(display_minutes) + 1, 5):
        col = int(m / display_minutes * graph_w)
        t   = start + timedelta(minutes=m)
        marks[min(col, graph_w - 5)] = t.strftime("%H:%M")
    line = " " * graph_w
    for col, label in sorted(marks.items()):
        line = line[:col] + label + line[col + len(label):]
    return line

def draw(rows, current):
    cols, _  = shutil.get_terminal_size()
    graph_w  = max(30, cols - 10)
    now      = datetime.now()

    # use actual data span, capped at DISPLAY_MIN
    if rows:
        data_span_min = (now - rows[0][0]).total_seconds() / 60
        effective_min = max(1, min(DISPLAY_MIN, data_span_min + 0.5))
    else:
        effective_min = DISPLAY_MIN

    buckets = bucket(rows, effective_min, graph_w)

    print("\033[2J\033[H", end="")

    # header
    start_label = (now - timedelta(minutes=effective_min)).strftime("%H:%M")
    print(f"  Temp Monitor  {now.strftime('%H:%M:%S')}   "
          f"window={effective_min:.0f}min  interval={INTERVAL}s  Ctrl+C exit")
    print(f"  {start_label} → now")
    print()

    # graph
    for row in range(GRAPH_H, -1, -1):
        t_at_row = MIN_T + (MAX_T - MIN_T) * row / GRAPH_H
        prefix   = f"{t_at_row:5.0f}°│"
        line     = prefix
        for b in buckets:
            char = " "
            for label, _, _, color in SENSORS:
                v = b.get(label)
                if v is not None:
                    frac   = (v - MIN_T) / (MAX_T - MIN_T)
                    filled = frac * GRAPH_H
                    if filled >= row:
                        char = f"{color}█{RESET}"
                        break
            line += char
        print(line)

    print(f"      └{'─' * graph_w}┐ now")
    print(f"       {x_axis_labels(effective_min, graph_w)}")
    print()

    # legend
    for label, _, _, color in SENSORS:
        print(f"  {color}█{RESET} {label}")
    print()

    # current values
    print("  Current:")
    for label, _, _, _ in SENSORS:
        val = current.get(label)
        c   = temp_color(val)
        b   = bar(val)
        v   = f"{val:5.1f}°C" if val is not None else "  N/A  "
        print(f"  {label}  {c}{b}{RESET}  {c}{v}{RESET}")

# ── main ─────────────────────────────────────────────────────────────────────

def main():
    rows   = load_history()
    prune_counter = 0

    while True:
        now     = datetime.now()
        current = read_sensors()
        rows.append((now, current))
        append_row(now, current)

        # keep in-memory list within window
        cutoff = now - timedelta(hours=WINDOW_H)
        rows   = [(ts, v) for ts, v in rows if ts >= cutoff]

        # prune file every 10 minutes
        prune_counter += 1
        if prune_counter >= 60:
            prune_file()
            prune_counter = 0

        draw(rows, current)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\033[2J\033[H")

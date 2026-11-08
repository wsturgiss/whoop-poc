#!/usr/bin/env python3
"""whoop-poc: heart rate + (approximate) sleep from a local NOOP whoop.db.

Reads the raw frames that NOOP's linux-capture `whoop_sync.py` stores in SQLite (read-only),
decodes WHOOP 4.0 type-47 HISTORICAL_DATA records (v12/v24 layout), and shows:

  * heart rate: latest, per-minute trend, min/avg/max, approximate resting HR
  * sleep: windows detected by a simple motion + HR heuristic (NOT WHOOP's sleep staging)

Subcommands:  tui (default) | summary | html | json (Coach snapshot) | serve (localhost JSON)
Core + HTML are stdlib only; the TUI (tui.py) uses Textual. Does no BLE itself; `--sync` shells out to `whoop-sync` / linux-capture's whoop_sync.py.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import sqlite3
import statistics as st
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

def _load_config() -> None:
    """Fill os.environ from ~/.config/whoop-poc/env (KEY=VALUE lines, the file whoop-sync sources) for
    keys not already set, so the TUI and whoop-sync share one config (WHOOP_MAX_HR, WHOOP_LIVE_MINUTES…)."""
    p = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "whoop-poc" / "env"
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return
    for ln in lines:
        k, sep, v = ln.strip().partition("=")
        if sep and k and not k.startswith("#") and k.isidentifier():
            os.environ.setdefault(k, v.strip().strip("'\""))


_load_config()

DEFAULT_DB = Path(os.environ.get(
    "WHOOP_DB", "~/src/noop/tools/linux-capture/captures/whoop.db")).expanduser()

# --- WHOOP 4.0 type-47 HISTORICAL_DATA, v12 == v24 layout (offsets into the full frame) ---------
# Source: NOOP Packages/WhoopProtocol/Sources/WhoopProtocol/Resources/whoop_protocol.json
TYPE_HISTORICAL = 47
OFF_VERSION, OFF_UNIX, OFF_HR, OFF_GRAV = 5, 11, 21, 40
SUPPORTED_VERSIONS = {12, 24}

# --- sleep heuristic knobs (approximate; tuned on very little data) -----------------------------
STILL_MOTION = 0.05      # mean per-second |Δgravity| (g, L1) below this = a "still" minute
MIN_WORN_SAMPLES = 20    # minute needs this many HR>0 seconds to count as worn
MERGE_GAP_MIN = 20       # restless gaps up to this long stay inside one sleep window
MIN_SLEEP_MIN = 60       # shorter still-and-calm runs are not called sleep (sitting still ≠ sleep)
CALM_MARGIN_BPM = 5      # "calm" = at least this far below the awake (moving) reference HR
MIN_ASLEEP_FRAC = 0.6    # a window must be mostly still-and-calm minutes
STAGES = ("wake", "rem", "light", "deep")   # display order, top → bottom
STAGER_BIN = Path(os.environ.get("WHOOP_STAGE", Path(__file__).resolve().parent / "stager/.build/release/whoop-stage"))
DEMO_END = 1577876400    # 2020-01-01 07:00 EST — demo night is pinned far from real data


@dataclass
class Sample:
    unix: int
    hr: int
    gx: float
    gy: float
    gz: float


@dataclass
class Minute:
    t: int                      # minute start (unix)
    hr: float | None            # mean of nonzero HR
    motion: float               # mean per-second |Δgravity|
    n: int                      # worn samples
    asleep: bool = False        # sleep-candidate minute


@dataclass
class SleepWindow:
    start: int
    end: int                    # exclusive, unix
    minutes: list[Minute] = field(default_factory=list)
    stages: list[dict] | None = None      # NOOP SleepStager segments: {start,end,stage}
    resting_hr: int | None = None         # NOOP: lowest 5-min rolling HR
    avg_hrv: float | None = None          # NOOP: mean RMSSD (ms)

    @property
    def span_min(self) -> int:
        return (self.end - self.start) // 60

    def stage_at(self, t: int) -> str | None:
        return next((g["stage"] for g in self.stages or () if g["start"] <= t < g["end"]), None)

    def stage_minutes(self) -> dict[str, int]:
        out = {k: 0 for k in STAGES}
        for g in self.stages or ():
            out[g["stage"]] = out.get(g["stage"], 0) + (g["end"] - g["start"]) // 60
        return out

    @property
    def asleep_min(self) -> int:
        if self.stages is not None:
            sm = self.stage_minutes()
            return sum(v for k, v in sm.items() if k != "wake")
        return sum(m.asleep for m in self.minutes)

    @property
    def hrs(self) -> list[float]:
        if self.stages is not None:
            return [m.hr for m in self.minutes if m.hr and self.stage_at(m.t + 30) not in (None, "wake")]
        return [m.hr for m in self.minutes if m.asleep and m.hr]


def decode_frame(raw: bytes) -> Sample | None:
    """Decode one type-47 record; None if unsupported or physically implausible."""
    if len(raw) < OFF_GRAV + 12 or raw[4] != TYPE_HISTORICAL or raw[OFF_VERSION] not in SUPPORTED_VERSIONS:
        return None
    unix = struct.unpack_from("<I", raw, OFF_UNIX)[0]
    gx, gy, gz = struct.unpack_from("<3f", raw, OFF_GRAV)
    mag = (gx * gx + gy * gy + gz * gz) ** 0.5
    if not 0.5 <= mag <= 1.5:      # same sanity gate NOOP uses: real gravity vector is ~1 g
        return None
    return Sample(unix, raw[OFF_HR], gx, gy, gz)


def load_samples(db: Path, since: int, until: int, address: str | None = None) -> tuple[list[Sample], dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dev = conn.execute(
            "SELECT id,address,name FROM devices " + ("WHERE address=? " if address else "")
            + "ORDER BY id LIMIT 1", (address,) if address else ()).fetchone()
        if not dev:
            raise SystemExit(f"no device{' ' + address if address else ''} in {db}")
        rows = conn.execute(
            "SELECT hex FROM frames WHERE device_id=? AND inner_type=? AND unix BETWEEN ? AND ? ORDER BY unix",
            (dev[0], TYPE_HISTORICAL, since, until)).fetchall()
        last = conn.execute("SELECT max(recv_ms) FROM frames WHERE device_id=?", (dev[0],)).fetchone()[0]
        newest = conn.execute("SELECT max(unix) FROM frames WHERE device_id=? AND inner_type=?",
                              (dev[0], TYPE_HISTORICAL)).fetchone()[0]
        try:   # ground-truth labels written by `whoop_sync.py label` (e.g. activity=awake)
            labels = conn.execute("SELECT start_unix,end_unix,activity FROM labels WHERE device_id=? "
                                  "AND end_unix>=? AND start_unix<=?", (dev[0], since, until)).fetchall()
        except sqlite3.OperationalError:
            labels = []
    finally:
        conn.close()
    seen, out = set(), []
    for (h,) in rows:
        s = decode_frame(bytes.fromhex(h))
        if s and s.unix not in seen:
            seen.add(s.unix)
            out.append(s)
    out.sort(key=lambda s: s.unix)
    return out, {"address": dev[1], "name": dev[2], "last_sync": (last or 0) // 1000, "newest_unix": newest,
                 "labels": labels}


def per_minute(samples: list[Sample]) -> list[Minute]:
    buckets: dict[int, list[tuple[int, float]]] = {}
    prev = None
    for s in samples:
        motion = 0.0
        if prev is not None and s.unix - prev.unix <= 2:
            motion = abs(s.gx - prev.gx) + abs(s.gy - prev.gy) + abs(s.gz - prev.gz)
        prev = s
        buckets.setdefault(s.unix // 60 * 60, []).append((s.hr, motion))
    mins = []
    for t in sorted(buckets):
        v = buckets[t]
        hrs = [h for h, _ in v if h > 0]
        mins.append(Minute(t, st.mean(hrs) if hrs else None, st.mean(m for _, m in v), len(hrs)))
    return mins


def detect_sleep(mins: list[Minute], labels: list[tuple[int, int, str]] = ()) -> list[SleepWindow]:
    """Approximate sleep: worn + still + HR well below the awake (moving) reference, merged over short
    gaps; windows must be ≥MIN_SLEEP_MIN long and mostly still-and-calm. Minutes inside an `awake`
    label (NOOP labels table) are never sleep."""
    awake = [(a, b) for a, b, act in labels if act.lower() in ("awake", "wake")]
    worn = [m for m in mins if m.n >= MIN_WORN_SAMPLES and m.hr]
    if not worn:
        return []
    active = [m.hr for m in worn if m.motion >= STILL_MOTION]
    # reference "awake" HR: median of moving minutes; fallback when we barely saw him move
    hr_ref = st.median(active) if len(active) >= 10 else sorted(m.hr for m in worn)[int(len(worn) * 0.9)]
    for m in mins:
        m.asleep = (m in worn and m.motion < STILL_MOTION and m.hr <= hr_ref - CALM_MARGIN_BPM
                    and not any(a <= m.t < b for a, b in awake))
    windows: list[SleepWindow] = []
    cur: SleepWindow | None = None
    for m in (m for m in mins if m.asleep):
        if cur and m.t - cur.end <= MERGE_GAP_MIN * 60:
            cur.end = m.t + 60
        else:
            cur = SleepWindow(m.t, m.t + 60)
            windows.append(cur)
    keep = []
    for w in windows:
        w.minutes = [m for m in mins if w.start <= m.t < w.end]
        if w.span_min >= MIN_SLEEP_MIN and w.asleep_min >= MIN_ASLEEP_FRAC * w.span_min:
            keep.append(w)
    return keep


@dataclass
class Report:
    meta: dict
    since: int
    until: int
    samples: list[Sample]
    minutes: list[Minute]
    sleep: list[SleepWindow]

    @property
    def hr_now(self) -> Sample | None:
        return next((s for s in reversed(self.samples) if s.hr > 0), None)

    @property
    def hr_minutes(self) -> list[Minute]:
        return [m for m in self.minutes if m.hr and m.n >= 5]

    def hr_stats(self) -> dict | None:
        hm = self.hr_minutes
        if not hm:
            return None
        vals = [m.hr for m in hm]
        still = sorted(m.hr for m in hm if m.motion < STILL_MOTION) or sorted(vals)
        return {"min": min(vals), "avg": st.mean(vals), "max": max(vals),
                "rest": still[max(0, len(still) // 20)],   # ~5th pct of still minutes
                "worn_min": len(hm)}


def demo_samples(end: int) -> list[Sample]:
    """Synthetic evening + 7.5h night ending at `end` (for --demo and tests). Not real data."""
    import math, random
    rnd, out = random.Random(4), []
    start, bed, wake = end - 10 * 3600, end - 8 * 3600, end - 30 * 60
    for t in range(start, end):
        sleeping = bed <= t < wake and not (bed + 3 * 3600 <= t < bed + 3 * 3600 + 900)  # one wake-up
        hr = (54 + 4 * math.sin(t / 2400) if sleeping else 72 + 10 * math.sin(t / 600)) + rnd.gauss(0, 1.5)
        jit = 0.002 if sleeping else 0.25
        gx, gy = 0.1 + rnd.gauss(0, jit), -0.4 + rnd.gauss(0, jit)
        gz = max(0.0, 1 - gx * gx - gy * gy) ** 0.5
        out.append(Sample(t, int(hr), gx, gy, gz))
    return out


def noop_stage(db: Path, since: int, until: int, address: str | None, mins: list[Minute],
               labels=()) -> list[SleepWindow] | None:
    """Run NOOP's SleepStager (via stager/whoop-stage) on NOOP's decoded feat_* tables.
    None = stager not built / no decoded data (caller falls back to the heuristic)."""
    if not STAGER_BIN.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dev = conn.execute("SELECT id FROM devices " + ("WHERE address=? " if address else "")
                           + "ORDER BY id LIMIT 1", (address,) if address else ()).fetchone()
        sec = conn.execute("SELECT unix,hr,gx,gy,gz,resp_raw FROM feat_second WHERE device_id=? "
                           "AND unix BETWEEN ? AND ? ORDER BY unix", (dev[0], since, until)).fetchall()
        rr = conn.execute("SELECT unix,rr_ms FROM feat_rr WHERE device_id=? AND unix BETWEEN ? AND ? "
                          "ORDER BY unix,idx", (dev[0], since, until)).fetchall()
    except sqlite3.OperationalError:     # NOOP decode never ran on this DB
        return None
    finally:
        conn.close()
    if not sec:
        return None
    return run_stager({
        "hr": [{"ts": u, "bpm": h} for u, h, *_ in sec if h],
        "gravity": [{"ts": u, "x": x, "y": y, "z": z} for u, _, x, y, z, _ in sec if x is not None],
        "resp": [{"ts": u, "raw": r} for u, *_, r in sec if r is not None],
        "rr": [{"ts": u, "rrMs": v} for u, v in rr],
        "tzOffsetSeconds": int(datetime.fromtimestamp(until).astimezone().utcoffset().total_seconds()),
    }, mins, labels)


def run_stager(payload: dict, mins: list[Minute], labels=()) -> list[SleepWindow] | None:
    import json
    if not STAGER_BIN.exists():
        return None
    res = subprocess.run([str(STAGER_BIN)], input=json.dumps(payload), capture_output=True, text=True)
    if res.returncode != 0:
        print(f"whoop-stage failed: {res.stderr.strip()[:200]}", file=sys.stderr)
        return None
    awake = [(a, b) for a, b, act in labels if act.lower() in ("awake", "wake")]
    out = []
    for s in json.loads(res.stdout):
        span = max(1, s["end"] - s["start"])
        covered = sum(max(0, min(b, s["end"]) - max(a, s["start"])) for a, b in awake)
        if covered * 2 > span:          # ground truth says he was awake for most of it
            continue
        out.append(SleepWindow(s["start"], s["end"], [m for m in mins if s["start"] <= m.t < s["end"]],
                               stages=s["stages"], resting_hr=s.get("restingHR"), avg_hrv=s.get("avgHRV")))
    return out


def demo_payload(samples: list[Sample]) -> dict:
    """Stager input for the synthetic night: HR + gravity + made-up beat intervals (≈1/s)."""
    import random
    rnd = random.Random(7)
    rr = [{"ts": x.unix, "rrMs": int(60000 / max(x.hr, 30) + rnd.gauss(0, 25 if x.hr < 62 else 8))}
          for x in samples if x.hr]
    return {"hr": [{"ts": x.unix, "bpm": x.hr} for x in samples if x.hr],
            "gravity": [{"ts": x.unix, "x": x.gx, "y": x.gy, "z": x.gz} for x in samples],
            "rr": rr, "resp": [], "tzOffsetSeconds": -18000}


def build_report(db: Path, hours: float, address: str | None, now: int | None = None,
                 demo: bool = False, heuristic: bool = False) -> Report:
    until = now or (DEMO_END if demo else int(time.time()))
    since = until - int(hours * 3600)
    if demo:   # pinned to a 2020 date so it can never be mistaken for the wearer's own night
        samples = [s for s in demo_samples(until) if s.unix >= since]
        mins = per_minute(samples)
        staged = None if heuristic else run_stager(demo_payload(samples), mins)
        return Report({"address": "-", "name": "DEMO — synthetic data, NOT your night", "last_sync": until, "newest_unix": until,
                       "labels": [], "sleep_source": "noop" if staged is not None else "heuristic"},
                      since, until, samples, mins, staged if staged is not None else detect_sleep(mins))
    samples, meta = load_samples(db, since, until + 300, address)
    mins = per_minute(samples)
    staged = None if heuristic else noop_stage(db, since, until + 300, address, mins, meta["labels"])
    meta["sleep_source"] = "noop" if staged is not None else "heuristic"
    sleep = staged if staged is not None else detect_sleep(mins, meta["labels"])
    return Report(meta, since, until, samples, mins, sleep)


# ------------------------------------------------------------------------------------- formatting
SPARK = "▁▂▃▄▅▆▇█"
SLEEP_SOURCE_LABEL = {
    "noop": "(NOOP SleepStager: approximate 4-stage, not PSG-validated)",
    "heuristic": "(approximate: motion + HR heuristic, not WHOOP staging)",
}
STAGE_CHAR = {"wake": "·", "rem": "▃", "light": "▅", "deep": "█"}
STAGE_COLOR = {"wake": "#f59e0b", "rem": "#a855f7", "light": "#38bdf8", "deep": "#2563eb"}


def no_sleep_msg(r: "Report") -> str:
    if not any(m.n for m in r.minutes):
        return "No sleep yet: no worn data in this window."
    if r.meta.get("sleep_source") == "noop":
        return "No sleep session yet: NOOP's stager hasn't found an in-bed stretch. Wear it overnight, then sync."
    return f"No sleep window yet: needs ≥{MIN_SLEEP_MIN} min still with calm HR. Wear it overnight, then sync."


def stage_bar(s: "SleepWindow", width: int, markup: bool = False) -> str:
    """One character per time bucket, showing the stage at the bucket's midpoint."""
    step = max(1, (s.end - s.start) / max(1, width))
    out = []
    for i in range(width):
        st_ = s.stage_at(int(s.start + (i + 0.5) * step))
        ch = STAGE_CHAR.get(st_, " ")
        out.append(f"[{STAGE_COLOR[st_]}]{ch}[/]" if markup and st_ else ch)
    return "".join(out)


def hhmm(t: int) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


def stamp(t: int) -> str:
    return datetime.fromtimestamp(t).strftime("%a %b %d %H:%M") if t else "never"


def dur(mins: int) -> str:
    return f"{mins // 60}h {mins % 60:02d}m"


def ago(t: int) -> str:
    s = max(0, int(time.time()) - t)
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago" if s < 2 * 86400 else f"{s // 86400}d {s % 86400 // 3600}h ago"


def spark(values: list[float | None], lo: float, hi: float) -> str:
    rng = max(hi - lo, 1)
    return "".join(" " if v is None else SPARK[min(7, int((v - lo) / rng * 7.999))] for v in values)


def bucket(minutes: list[Minute], start: int, end: int, width: int) -> list[list[Minute]]:
    step = max(60, (end - start) / max(width, 1))
    out: list[list[Minute]] = [[] for _ in range(width)]
    for m in minutes:
        i = int((m.t - start) / step)
        if 0 <= i < width:
            out[i].append(m)
    return out


def text_lines(r: Report, width: int = 80) -> list[tuple[str, str]]:
    """(style, text) lines shared by `summary` and the TUI. style: title|head|dim|hr|sleep|''."""
    w = max(40, width - 2)
    L: list[tuple[str, str]] = []
    L.append(("title", f" WHOOP PoC · {r.meta.get('name') or r.meta['address']}"))
    L.append(("dim", f" window: last {(r.until - r.since) // 3600}h   last sync: {stamp(r.meta['last_sync'])}"
                     f" ({ago(r.meta['last_sync'])})"))
    L.append(("", ""))

    L.append(("head", " HEART RATE"))
    now, stats = r.hr_now, r.hr_stats()
    if not now or not stats:
        L.append(("dim", "  no heart-rate data in this window — wear the strap, then sync"))
    else:
        L.append(("hr", f"  ♥ {now.hr} bpm   at {hhmm(now.unix)} ({ago(now.unix)})"))
        L.append(("", f"  min {stats['min']:.0f}  avg {stats['avg']:.0f}  max {stats['max']:.0f}   "
                      f"resting≈{stats['rest']:.0f}   worn {dur(stats['worn_min'])}"))
        hm = r.hr_minutes
        lo, hi = min(m.hr for m in hm), max(m.hr for m in hm)
        # trend over the span that actually has data (so 15 min of data isn't a dot on a 24h axis)
        t0, t1 = hm[0].t, hm[-1].t + 60
        cols = max(12, min(w - 2, (t1 - t0) // 60))
        bins = bucket(hm, t0, t1, cols)
        vals = [st.mean(m.hr for m in b) if b else None for b in bins]
        L.append(("hr", "  " + spark(vals, lo, hi)))
        L.append(("dim", f"  {hhmm(t0)}{'':{max(1, cols - 10)}}{hhmm(t1)}   ({lo:.0f}–{hi:.0f} bpm)"))
    L.append(("", ""))

    L.append(("head", " SLEEP  " + SLEEP_SOURCE_LABEL[r.meta.get("sleep_source", "heuristic")]))
    if not r.sleep:
        L.append(("dim", "  " + no_sleep_msg(r)))
    for s in reversed(r.sleep[-3:]):
        hrs = s.hrs
        eff = 100 * s.asleep_min / max(1, s.span_min)
        L.append(("sleep", f"  {stamp(s.start)} → {hhmm(s.end)}   in bed {dur(s.span_min)}   "
                           f"asleep≈{dur(s.asleep_min)} ({eff:.0f}%)"))
        if hrs:
            L.append(("", f"    sleeping HR avg {st.mean(hrs):.0f}  low {min(hrs):.0f} bpm"))
        if s.stages is not None:
            sm = s.stage_minutes()
            extra = (f"   resting {s.resting_hr}" if s.resting_hr else "") + \
                    (f"   HRV {s.avg_hrv:.0f} ms" if s.avg_hrv else "")
            L.append(("", "    " + "  ".join(f"{k} {dur(sm[k])}" for k in ("deep", "light", "rem", "wake")) + extra))
            L.append(("sleep", "    " + stage_bar(s, w - 4)))
            L.append(("dim", "    " + "  ".join(f"{STAGE_CHAR[k]} {k}" for k in STAGES)))
            continue
        bins = bucket(s.minutes, s.start, s.end, w - 4)
        L.append(("sleep", "    " + "".join(
            " " if not b else "█" if sum(m.asleep for m in b) * 2 >= len(b) else "░" for b in bins)))
        L.append(("dim", "    █ still+calm  ░ restless/awake"))
    return L


# ------------------------------------------------------------------------------------------ HTML
def render_html(r: Report, refresh: int | None = None) -> str:
    e = html.escape
    # Every clock time carries its unix ts in data-t; the inline script re-renders it in the BROWSER's
    # timezone. The server-side text (this machine's local zone) is only the no-JS fallback.
    T = lambda t, f="hm": f'<time data-t="{int(t)}" data-f="{f}">{e(hhmm(int(t)) if f == "hm" else stamp(int(t)))}</time>'
    local_tz = datetime.now().astimezone().tzname() or "local"
    hm, stats, now = r.hr_minutes, r.hr_stats(), r.hr_now
    W, H, PAD = 900, 260, 36
    svg = '<p class="dim">No heart-rate data in this window yet.</p>'
    if hm:
        t0, t1 = hm[0].t, hm[-1].t + 60
        for s in r.sleep:
            t0, t1 = min(t0, s.start), max(t1, s.end)
        lo, hi = min(m.hr for m in hm) - 5, max(m.hr for m in hm) + 5
        x = lambda t: PAD + (t - t0) / max(1, t1 - t0) * (W - 2 * PAD)
        y = lambda v: H - PAD - (v - lo) / max(1, hi - lo) * (H - 2 * PAD)
        bands = "".join(f'<rect class="band" x="{x(s.start):.1f}" y="{PAD}" width="{x(s.end) - x(s.start):.1f}" '
                        f'height="{H - 2 * PAD}"><title data-range="{s.start},{s.end}">approx. sleep {hhmm(s.start)}–{hhmm(s.end)}</title></rect>'
                        for s in r.sleep)
        segs, cur, prev = [], [], None
        for m in hm:                                      # break the line across gaps > 5 min
            if prev and m.t - prev > 300 and cur:
                segs.append(cur); cur = []
            cur.append(f"{x(m.t + 30):.1f},{y(m.hr):.1f}"); prev = m.t
        segs.append(cur)
        lines = "".join(f'<polyline class="hr" points="{" ".join(s)}"/>' for s in segs if s)
        dots = "".join(f'<circle class="hr" cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="2.5"/>'
                       for s in segs if len(s) == 1 for p in s)
        ticks = ""
        for i in range(5):
            v = lo + (hi - lo) * i / 4
            ticks += (f'<line class="grid" x1="{PAD}" x2="{W - PAD}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                      f'<text class="ax" x="{PAD - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:.0f}</text>')
        for i in range(5):
            t = t0 + (t1 - t0) * i / 4
            ticks += f'<text class="ax" x="{x(t):.1f}" y="{H - PAD + 18}" text-anchor="middle" data-t="{int(t)}" data-f="hm">{hhmm(int(t))}</text>'
        svg = (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Heart rate per minute">'
               f'{bands}{ticks}{lines}{dots}</svg>')

    def tile(label, value, unit="", note="", note_html=None):
        return (f'<div class="tile"><div class="lbl">{e(label)}</div><div class="val">{e(value)}'
                f'<span class="unit">{e(unit)}</span></div><div class="dim">{note_html or e(note)}</div></div>')

    tiles = ""
    if now and stats:
        tiles = (tile("Latest HR", f"{now.hr}", " bpm", note_html=f"{T(now.unix)} · {e(ago(now.unix))}")
                 + tile("Resting HR ≈", f"{stats['rest']:.0f}", " bpm", "5th pct of still minutes")
                 + tile("Range", f"{stats['min']:.0f}–{stats['max']:.0f}", " bpm", f"avg {stats['avg']:.0f}")
                 + tile("Worn", dur(stats["worn_min"]), "", f"last {(r.until - r.since) // 3600}h"))
    staged = r.meta.get("sleep_source") == "noop"
    st_cols = "".join(f"<th>{k.capitalize()}</th>" for k in ("deep", "light", "rem", "wake")) if staged else ""

    def row(s):
        cells = (f"<td>{T(s.start, 'stamp')} → {T(s.end)}</td><td>{dur(s.span_min)}</td><td>{dur(s.asleep_min)}</td>"
                 f"<td>{(st.mean(s.hrs) if s.hrs else 0):.0f}</td><td>{(min(s.hrs) if s.hrs else 0):.0f}</td>")
        if staged:
            sm = s.stage_minutes()
            cells += "".join(f"<td>{dur(sm[k])}</td>" for k in ("deep", "light", "rem", "wake"))
        return f"<tr>{cells}</tr>"

    def hypnogram(s):
        HW, LH, LP = 900, 26, 56                    # width, lane height, left pad for lane labels
        x = lambda t: LP + (t - s.start) / max(1, s.end - s.start) * (HW - LP - 10)
        lanes = "".join(f'<text class="ax" x="{LP - 8}" y="{i * LH + LH * .65:.1f}" text-anchor="end">{k.upper()}</text>'
                        f'<line class="grid" x1="{LP}" x2="{HW - 10}" y1="{(i + 1) * LH}" y2="{(i + 1) * LH}"/>'
                        for i, k in enumerate(STAGES))
        rects = "".join(
            f'<rect x="{x(g["start"]):.1f}" y="{STAGES.index(g["stage"]) * LH + 3}" '
            f'width="{max(.8, x(g["end"]) - x(g["start"])):.1f}" height="{LH - 6}" rx="2" fill="{STAGE_COLOR[g["stage"]]}">'
            f'<title data-range="{g["start"]},{g["end"]}" data-label="{g["stage"]}">{g["stage"]}</title></rect>'
            for g in s.stages or () if g["stage"] in STAGES)
        ticks = "".join(f'<text class="ax" x="{x(t):.1f}" y="{len(STAGES) * LH + 16}" text-anchor="middle" '
                        f'data-t="{int(t)}" data-f="hm">{hhmm(int(t))}</text>'
                        for t in (s.start + (s.end - s.start) * i / 4 for i in range(5)))
        H2 = len(STAGES) * LH + 24
        return (f'<div class="hyp"><div class="dim">{T(s.start, "stamp")} → {T(s.end)} · '
                f'resting HR {s.resting_hr or "—"} · HRV {f"{s.avg_hrv:.0f} ms" if s.avg_hrv else "—"}</div>'
                f'<svg viewBox="0 0 {HW} {H2}" role="img" aria-label="Sleep stages">{lanes}{rects}{ticks}</svg></div>')

    legend = " ".join(f'<span class="key"><i style="background:{STAGE_COLOR[k]}"></i>{k}</span>' for k in STAGES)
    hyps = (f'<div class="dim">Stages from NOOP SleepStager · time axis <span class="tz">{e(local_tz)}</span> · {legend}</div>'
            + "".join(hypnogram(s) for s in reversed(r.sleep[-3:]))) if staged and r.sleep else ""
    sleep_html = (f'{hyps}<table><tr><th>Window <span class="tz">{e(local_tz)}</span></th><th>In bed</th>'
                  f'<th>Asleep ≈</th><th>Avg HR</th><th>Low HR</th>{st_cols}</tr>'
                  + "".join(row(s) for s in reversed(r.sleep)) + '</table>' if r.sleep else
                  f'<p class="dim">{e(no_sleep_msg(r))}</p>')
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}<title>Whoop PoC</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--dim:#78716c;--card:#fff;--line:#e7e5e4;--hr:#dc2626;--sleep:#6366f155}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0c0a09;--fg:#f5f5f4;--dim:#a8a29e;--card:#1c1917;
--line:#292524;--hr:#f87171;--sleep:#818cf844}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:960px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0}} h2{{font-size:16px;margin:28px 0 8px}}
.dim{{color:var(--dim);font-size:13px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:16px}}
.tile,.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.lbl{{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.val{{font-size:28px;font-weight:600;font-variant-numeric:tabular-nums}} .unit{{font-size:14px;color:var(--dim)}}
svg{{width:100%;height:auto;display:block}}
polyline.hr{{fill:none;stroke:var(--hr);stroke-width:2;stroke-linejoin:round}} circle.hr{{fill:var(--hr)}}
.grid{{stroke:var(--line)}} .ax{{fill:var(--dim);font-size:11px}} .band{{fill:var(--sleep)}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}}
.hyp{{margin:10px 0 18px}} .key{{margin-left:10px;white-space:nowrap}}
.key i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}}
.card{{overflow-x:auto}} th{{color:var(--dim);font-weight:500;font-size:13px}}
</style></head><body><main>
<h1>Whoop PoC · {e(r.meta.get('name') or r.meta['address'])}</h1>
<div class="dim">Last {(r.until - r.since) // 3600}h · last sync {T(r.meta['last_sync'], 'stamp')} · generated {T(time.time(), 'stamp')} · times shown in <span class="tz">{e(local_tz)}</span></div>
<div class="tiles">{tiles}</div>
<h2>Heart rate (per-minute average)</h2><div class="card">{svg}
<div class="dim">Time axis: <span class="tz">{e(local_tz)}</span>. Shaded areas are approximate sleep windows.</div></div>
<h2>Sleep <span class="dim">{e(SLEEP_SOURCE_LABEL[r.meta.get("sleep_source", "heuristic")])}</span></h2>
<div class="card">{sleep_html}</div>
</main>
<script>
(() => {{
  const fmt = {{hm: {{hour: "2-digit", minute: "2-digit", hour12: false}},
               stamp: {{weekday: "short", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}}}};
  const f = (t, k) => new Date(t * 1000).toLocaleString(undefined, fmt[k] || fmt.hm);
  document.querySelectorAll("[data-t]").forEach(el => el.textContent = f(+el.dataset.t, el.dataset.f));
  document.querySelectorAll("[data-range]").forEach(el => {{
    const [a, b] = el.dataset.range.split(",");
    el.textContent = `${{el.dataset.label || "approx. sleep"}} ${{f(+a, "hm")}}–${{f(+b, "hm")}}`;
  }});
  // zone label: IANA name + abbreviation(s) for the times actually on the page (EST vs EDT across DST)
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const abbr = t => new Intl.DateTimeFormat(undefined, {{timeZoneName: "short"}})
      .formatToParts(new Date(t * 1000)).find(p => p.type === "timeZoneName")?.value;
  const ts = [...document.querySelectorAll("[data-t]")].map(el => +el.dataset.t);
  const abbrs = [...new Set(ts.map(abbr))].filter(Boolean).join("/");
  document.querySelectorAll(".tz").forEach(el => el.textContent = abbrs ? `${{zone}} (${{abbrs}})` : zone);
}})();
</script>
</body></html>"""


def run_html(args) -> None:
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    while True:
        r = build_report(args.db, args.hours, args.address, demo=args.demo, heuristic=args.heuristic)
        out.write_text(render_html(r, args.refresh if args.watch else None))
        print(f"wrote {out}  ({len(r.samples)} samples, {len(r.sleep)} sleep window(s))", flush=True)
        if not args.watch:
            return
        time.sleep(args.refresh)


# ------------------------------------------------------------------------- Coach-readable snapshot
SNAPSHOT_SCHEMA = "whoop-poc/snapshot@1"     # bump on any breaking change; see COACH.md
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "whoop-poc"


def _iso(t: int | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def last_sync_attempt() -> dict | None:
    """What the whoop-sync wrapper recorded for its latest `sync` attempt (None if never)."""
    import json
    try:
        return json.loads((STATE_DIR / "sync.json").read_text())
    except (OSError, ValueError):
        return None


def last_battery() -> dict | None:
    """Strap battery from WHOOP's GET_BATTERY_LEVEL, read by whoop-sync after each sync, or None.
    Readings from the standard BLE Battery Service (flat 100%, untrustworthy) are ignored."""
    import json
    try:
        b = json.loads((STATE_DIR / "battery.json").read_text())
        ok = isinstance(b.get("pct"), int) and isinstance(b.get("unix"), int) and b.get("source") == "whoop-cmd-26"
        return b if ok else None
    except (OSError, ValueError, AttributeError):
        return None


def snapshot(r: Report, now: int | None = None) -> dict:
    """Stable JSON document for other programs (e.g. Coach). Schema documented in COACH.md."""
    now = now or int(time.time())
    hr_now, stats = r.hr_now, r.hr_stats()
    newest = r.samples[-1].unix if r.samples else None
    sessions = []
    for s in r.sleep:
        hrs = s.hrs
        sessions.append({
            "start_unix": s.start, "end_unix": s.end, "start_iso": _iso(s.start), "end_iso": _iso(s.end),
            "in_bed_min": s.span_min, "asleep_min": s.asleep_min,
            "stage_min": s.stage_minutes() if s.stages is not None else None,
            "avg_hr": round(st.mean(hrs), 1) if hrs else None,
            "resting_hr": s.resting_hr, "avg_hrv_ms": round(s.avg_hrv, 1) if s.avg_hrv else None,
            "stages": [{"start_unix": g["start"], "end_unix": g["end"], "stage": g["stage"]}
                       for g in s.stages] if s.stages is not None else None,
        })
    return {
        "schema": SNAPSHOT_SCHEMA,
        "demo": r.meta.get("address") == "-",
        "generated_unix": now, "generated_iso": _iso(now),
        "window": {"since_unix": r.since, "until_unix": r.until, "hours": round((r.until - r.since) / 3600, 2)},
        "device": {"address": r.meta.get("address"), "name": r.meta.get("name")},
        "freshness": {
            "newest_sample_unix": newest, "newest_sample_iso": _iso(newest),
            "data_age_min": None if newest is None else round((now - newest) / 60, 1),
            "last_received_unix": r.meta.get("last_sync") or None,
            "last_sync_attempt": last_sync_attempt(),
        },
        "hr": {
            "latest": None if hr_now is None else {"unix": hr_now.unix, "iso": _iso(hr_now.unix), "bpm": hr_now.hr},
            "stats": None if stats is None else {
                "min": round(stats["min"]), "avg": round(stats["avg"], 1), "max": round(stats["max"]),
                "resting_est": round(stats["rest"]), "worn_min": stats["worn_min"]},
            "per_minute": [[m.t, round(m.hr, 1)] for m in r.hr_minutes],
        },
        "sleep": {"source": r.meta.get("sleep_source"), "sessions": sessions},
        "battery": None if r.meta.get("address") == "-" else last_battery(),
        "labels": [{"start_unix": a, "end_unix": b, "activity": act} for a, b, act in r.meta.get("labels", [])],
    }


_PROGRESS = re.compile(r"sync \S+\s+(\d+)%.*?@(\d\d-\d\d \d\d:\d\d:\d\d)\s+([\d.]+) rec/s\s+ETA (\S+)\s+stored ([\d,]+)")


def drain_running() -> bool:
    """True while any `whoop_sync.py sync` process is alive (TUI `s`, timer, or manual)."""
    return drain_pid() is not None


def drain_pid() -> int | None:
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            argv = (p / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"whoop_sync.py") for a in argv) and b"sync" in argv:
            return int(p.name)
    return None


# ------------------------------------------------------------------------------------- live HR
LIVE_MINUTES = int(os.environ.get("WHOOP_LIVE_MINUTES", 30))   # live HR holds a BLE link open (strap
#                                                                  battery), so it always auto-stops


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def live_status() -> dict | None:
    """live.json from `whoop-sync live` while that process is running, else None."""
    import json
    try:
        d = json.loads((STATE_DIR / "live.json").read_text())
    except (OSError, ValueError):
        return None
    return d if d.get("state") != "ended" and _alive(d.get("pid")) else None


def start_live(minutes: float = LIVE_MINUTES) -> str:
    """Start live HR detached. A running drain is asked to stop first (it saves, decodes and resumes on
    the next timer run); `whoop-sync live` waits for the strap lock until it has."""
    import signal
    msg = "Live HR starting"
    if (pid := drain_pid()) is not None:
        os.kill(pid, signal.SIGTERM)
        msg = "Pausing the sync (it resumes later); live HR starting"
    (STATE_DIR / "live.json").unlink(missing_ok=True)
    subprocess.Popen(["whoop-sync", "live", "--minutes", str(minutes)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return msg


# HR zones as % of max HR (the common 5-zone model). Max HR is personal: set WHOOP_MAX_HR.
MAX_HR = int(os.environ.get("WHOOP_MAX_HR", 190))
ZONES = (  # (zone, name, low fraction of max HR, color), highest first
    ("Z5", "max", 0.90, "#ef4444"),
    ("Z4", "hard", 0.80, "#f97316"),
    ("Z3", "moderate", 0.70, "#eab308"),
    ("Z2", "light", 0.60, "#22c55e"),
    ("Z1", "very light", 0.50, "#38bdf8"),
)
REST_ZONE = ("Z0", "rest", 0.0, "#6b7280")
BLOCKS = "▁▂▃▄▅▆▇█"


def hr_zone(bpm: float, max_hr: int = MAX_HR) -> tuple[str, str, float, str]:
    return next((z for z in ZONES if bpm >= z[2] * max_hr), REST_ZONE)


def zone_range(z: tuple, max_hr: int = MAX_HR) -> str:
    """bpm range label for a zone, e.g. '133–151'."""
    order = (REST_ZONE,) + ZONES[::-1]
    i = order.index(z)
    lo = round(z[2] * max_hr)
    return f"<{round(ZONES[-1][2] * max_hr)}" if z is REST_ZONE else (
        f"{lo}+" if i == len(order) - 1 else f"{lo}–{round(order[i + 1][2] * max_hr) - 1}")


def live_samples() -> list[tuple[int, int]]:
    """The current/last live session's (unix, bpm) series, from live-samples.csv."""
    out = []
    try:
        for ln in (STATE_DIR / "live-samples.csv").read_text().splitlines():
            t, _, b = ln.partition(",")
            if t.isdigit() and b.isdigit():
                out.append((int(t), int(b)))
    except OSError:
        pass
    return out


def last_live() -> dict | None:
    """live.json whatever its state (running or ended)."""
    import json
    try:
        return json.loads((STATE_DIR / "live.json").read_text())
    except (OSError, ValueError):
        return None


def zone_seconds(samples: list[tuple[int, int]], max_hr: int = MAX_HR) -> dict[str, int]:
    """Seconds per zone; each sample counts until the next one (gaps over 5 s count as 1 s)."""
    out = {z[0]: 0 for z in (REST_ZONE,) + ZONES}
    for (t, b), nxt in zip(samples, samples[1:] + [None]):
        dt = 1 if nxt is None else nxt[0] - t
        out[hr_zone(b, max_hr)[0]] += dt if 0 < dt <= 5 else 1
    return out


def live_chart(samples: list[tuple[int, int]], width: int, height: int = 6, max_hr: int = MAX_HR,
               t1: int | None = None) -> list[str]:
    """Rich-markup bar chart of a live session: one column per time bucket (mean bpm), bar colored by
    HR zone, bpm labels on the left and a start → end time axis underneath."""
    if not samples or width < 10:
        return []
    lab = 4
    w = width - lab
    t0 = samples[0][0]
    t1 = max(t1 or samples[-1][0], t0 + 60)
    cols: list[list[int]] = [[] for _ in range(w)]
    for t, b in samples:
        cols[min(w - 1, int((t - t0) * w / (t1 - t0 + 1)))].append(b)
    vals = [sum(c) / len(c) if c else None for c in cols]
    have = [v for v in vals if v is not None]
    lo = int(min(have) - 5) // 10 * 10
    hi = -(-int(max(have) + 5) // 10) * 10
    lines = []
    for r in range(height):
        floor = (height - 1 - r) * 8
        cells = []
        for v in vals:
            if v is None:
                cells.append(" ")
                continue
            e = round((v - lo) / (hi - lo) * height * 8) - floor
            ch = BLOCKS[7] if e >= 8 else BLOCKS[e - 1] if e > 0 else " "
            cells.append(f"[{hr_zone(v, max_hr)[3]}]{ch}[/]" if ch != " " else " ")
        label = f"{hi:>3} " if r == 0 else f"{lo:>3} " if r == height - 1 else " " * lab
        lines.append(f"[dim]{label}[/]" + "".join(cells))
    a, b = hhmm(t0), hhmm(t1)
    lines.append("[dim]" + " " * lab + a + " " * max(1, w - len(a) - len(b)) + b + "[/]")
    return lines


def extend_live() -> bool:
    """+10 min on a running live session (whoop_live.py handles SIGUSR1; capped at 3 h total)."""
    import signal
    d = live_status()
    if not d:
        return False
    os.kill(int(d["pid"]), signal.SIGUSR1)
    return True


def stop_live() -> bool:
    import signal
    d = live_status()
    if not d:
        return False
    os.kill(int(d["pid"]), signal.SIGTERM)
    return True


def drain_status() -> dict | None:
    """None when no drain is running, else {phase, pct, at, rate, eta, stored} parsed from the
    tail of the wrapper's last-sync.log (fields are None until the first progress line)."""
    if not drain_running():
        return None
    out = {"phase": "connecting", "pct": None, "at": None, "rate": None, "eta": None, "stored": None}
    try:
        with open(STATE_DIR / "last-sync.log", "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            lines = f.read().decode(errors="replace").replace("\r", "\n").splitlines()
    except OSError:
        return out
    for ln in lines:
        if m := _PROGRESS.search(ln):
            pct, at, rate, eta, stored = m.groups()
            out.update(phase="draining", pct=int(pct), at=at, rate=float(rate), eta=eta,
                       stored=int(stored.replace(",", "")))
        elif "session ended" in ln or "acquire:" in ln:
            out["phase"] = "reconnecting" if out["pct"] is not None else "connecting"
        elif "stored this run" in ln or "decoded features" in ln:
            out["phase"] = "finishing"
    return out


def write_json(path: Path, doc: dict) -> None:
    """Atomic, owner-only write (biometric data)."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def run_serve(args) -> None:
    """Tiny localhost-only JSON endpoint: GET /v1/snapshot[?hours=N], GET /healthz."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/healthz":
                code, doc = 200, {"ok": True}
            elif u.path == "/v1/snapshot":
                try:
                    hours = float(parse_qs(u.query).get("hours", [args.hours])[0])
                    doc = snapshot(build_report(args.db, hours, args.address, demo=args.demo, heuristic=args.heuristic))
                    code = 200
                except (ValueError, SystemExit) as e:
                    code, doc = 400, {"error": str(e)}
            else:
                code, doc = 404, {"error": "try /v1/snapshot"}
            body = json.dumps(doc).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    print(f"serving http://127.0.0.1:{args.port}/v1/snapshot", flush=True)
    srv.serve_forever()


# ------------------------------------------------------------------------------------------ sync
def sync_cmd() -> list[str] | None:
    """Reuse linux-capture: prefer the `whoop-sync` wrapper, else call whoop_sync.py directly."""
    if shutil.which("whoop-sync"):
        return ["whoop-sync", "sync"]
    lc = Path(os.environ.get("WHOOP_CAPTURE_DIR", "~/src/noop/tools/linux-capture")).expanduser()
    addr = os.environ.get("WHOOP_ADDRESS")
    if not addr:
        return None
    return [str(lc / ".venv/bin/python"), str(lc / "whoop_sync.py"), "sync", "--model", "whoop4",
            "--address", addr, "--subject", "me", "--db", str(DEFAULT_DB), "--max", "1200"]


def run_sync(quiet: bool = False) -> bool:
    """Blocking drain (used by --sync before showing anything)."""
    cmd = sync_cmd()
    if not cmd:
        print("set WHOOP_ADDRESS (strap MAC) or install the whoop-sync wrapper", file=sys.stderr)
        return False
    out = subprocess.DEVNULL if quiet else None
    return subprocess.run(cmd, stdout=out, stderr=out).returncode == 0


def start_sync_background() -> tuple[bool, str]:
    """Start a drain the caller does NOT own, so a UI can quit while it runs (drains are resume-safe).
    Prefers the systemd unit (also refreshes coach.json); falls back to a detached process."""
    if drain_running():
        return True, "A drain is already running"
    r = subprocess.run(["systemctl", "--user", "start", "--no-block", "whoop-sync.service"],
                       capture_output=True)
    if r.returncode == 0:
        return True, "Sync started (whoop-sync.service)"
    cmd = sync_cmd()
    if not cmd:
        return False, "Set WHOOP_ADDRESS or run contrib/install.sh"
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True, "Sync started in the background"


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="whoop-poc", description=__doc__.splitlines()[0])
    p.add_argument("cmd", nargs="?", default="tui", choices=["tui", "summary", "html", "json", "serve"])
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"NOOP whoop.db (default {DEFAULT_DB}; env WHOOP_DB)")
    p.add_argument("--address", help="strap MAC if the DB holds several devices")
    p.add_argument("--hours", type=float, default=24, help="look-back window (default 24)")
    p.add_argument("--sync", action="store_true", help="drain new history from the strap first")
    p.add_argument("--refresh", type=int, default=30, help="TUI reload / html --watch interval, seconds")
    p.add_argument("--out", default=None, help="html: output path (default out/whoop.html); json: write here "
                   "atomically instead of stdout")
    p.add_argument("--port", type=int, default=8787, help="serve: localhost port (default 8787)")
    p.add_argument("--watch", action="store_true", help="html: regenerate every --refresh seconds")
    p.add_argument("--heuristic", action="store_true", help="skip NOOP's SleepStager; use the simple heuristic")
    p.add_argument("--demo", action="store_true", help="use a synthetic night instead of the DB (shows the sleep view)")
    a = p.parse_args(argv)
    a.db = a.db.expanduser()
    if not a.demo and not a.db.exists():
        raise SystemExit(f"db not found: {a.db}  (run a NOOP linux-capture sync first, or pass --db)")
    if a.sync and not run_sync():
        print("sync failed (is the strap nearby?) — showing what is already stored", file=sys.stderr)
    if a.cmd == "tui":
        from tui import run_tui      # Textual is only needed for the TUI
        run_tui(a)
    elif a.cmd == "summary":
        width = shutil.get_terminal_size((88, 24)).columns
        for _, line in text_lines(build_report(a.db, a.hours, a.address, demo=a.demo, heuristic=a.heuristic), width):
            print(line)
    elif a.cmd == "json":
        import json
        doc = snapshot(build_report(a.db, a.hours, a.address, demo=a.demo, heuristic=a.heuristic))
        if a.out:
            write_json(Path(a.out).expanduser(), doc)
        else:
            print(json.dumps(doc, indent=1))
    elif a.cmd == "serve":
        run_serve(a)
    else:
        a.out = a.out or "out/whoop.html"
        run_html(a)


if __name__ == "__main__":
    main()

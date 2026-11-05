#!/usr/bin/env python3
"""whoop-poc: heart rate + (approximate) sleep from a local NOOP whoop.db.

Reads the raw frames that NOOP's linux-capture `whoop_sync.py` stores in SQLite (read-only),
decodes WHOOP 4.0 type-47 HISTORICAL_DATA records (v12/v24 layout), and shows:

  * heart rate: latest, per-minute trend, min/avg/max, approximate resting HR
  * sleep: windows detected by a simple motion + HR heuristic (NOT WHOOP's sleep staging)

Subcommands:  tui (default) | summary | html
Core + HTML are stdlib only; the TUI (tui.py) uses Textual. Does no BLE itself; `--sync` shells out to `whoop-sync` / linux-capture's whoop_sync.py.
"""
from __future__ import annotations

import argparse
import html
import os
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

    @property
    def span_min(self) -> int:
        return (self.end - self.start) // 60

    @property
    def asleep_min(self) -> int:
        return sum(m.asleep for m in self.minutes)

    @property
    def hrs(self) -> list[float]:
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
    return out, {"address": dev[1], "name": dev[2], "last_sync": (last or 0) // 1000, "labels": labels}


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


def build_report(db: Path, hours: float, address: str | None, now: int | None = None,
                 demo: bool = False) -> Report:
    until = now or (DEMO_END if demo else int(time.time()))
    since = until - int(hours * 3600)
    if demo:   # pinned to a 2020 date so it can never be mistaken for the wearer's own night
        samples = [s for s in demo_samples(until) if s.unix >= since]
        mins = per_minute(samples)
        return Report({"address": "-", "name": "DEMO — synthetic data, NOT your night", "last_sync": until,
                       "labels": []}, since, until, samples, mins, detect_sleep(mins))
    samples, meta = load_samples(db, since, until + 300, address)
    mins = per_minute(samples)
    return Report(meta, since, until, samples, mins, detect_sleep(mins, meta["labels"]))


# ------------------------------------------------------------------------------------- formatting
SPARK = "▁▂▃▄▅▆▇█"


def hhmm(t: int) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


def stamp(t: int) -> str:
    return datetime.fromtimestamp(t).strftime("%a %b %d %H:%M") if t else "never"


def dur(mins: int) -> str:
    return f"{mins // 60}h {mins % 60:02d}m"


def ago(t: int) -> str:
    s = max(0, int(time.time()) - t)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago"


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

    L.append(("head", " SLEEP  (approximate: motion + HR heuristic, not WHOOP staging)"))
    if not r.sleep:
        need = "" if any(m.n for m in r.minutes) else " — no worn data in window"
        L.append(("dim", f"  no sleep window detected yet (needs ≥{MIN_SLEEP_MIN} min still + calm HR){need}"))
    for s in reversed(r.sleep[-3:]):
        hrs = s.hrs
        eff = 100 * s.asleep_min / max(1, s.span_min)
        L.append(("sleep", f"  {stamp(s.start)} → {hhmm(s.end)}   in bed {dur(s.span_min)}   "
                           f"asleep≈{dur(s.asleep_min)} ({eff:.0f}%)"))
        if hrs:
            L.append(("", f"    sleeping HR avg {st.mean(hrs):.0f}  low {min(hrs):.0f} bpm"))
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
    sleep_rows = "".join(
        f"<tr><td>{T(s.start, 'stamp')} → {T(s.end)}</td><td>{dur(s.span_min)}</td><td>{dur(s.asleep_min)}</td>"
        f"<td>{(st.mean(s.hrs) if s.hrs else 0):.0f}</td><td>{(min(s.hrs) if s.hrs else 0):.0f}</td></tr>"
        for s in reversed(r.sleep))
    sleep_html = (f'<table><tr><th>Window <span class="tz">{e(local_tz)}</span></th><th>In bed</th><th>Asleep ≈</th><th>Avg HR</th><th>Low HR</th></tr>'
                  f'{sleep_rows}</table>' if r.sleep else
                  f'<p class="dim">No sleep window detected yet. It needs at least {MIN_SLEEP_MIN} minutes of the '
                  f'strap being still with calm heart rate. Wear it overnight, then sync.</p>')
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
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}} th{{color:var(--dim);font-weight:500;font-size:13px}}
</style></head><body><main>
<h1>Whoop PoC · {e(r.meta.get('name') or r.meta['address'])}</h1>
<div class="dim">Last {(r.until - r.since) // 3600}h · last sync {T(r.meta['last_sync'], 'stamp')} · generated {T(time.time(), 'stamp')} · times shown in <span class="tz">{e(local_tz)}</span></div>
<div class="tiles">{tiles}</div>
<h2>Heart rate (per-minute average)</h2><div class="card">{svg}
<div class="dim">Time axis: <span class="tz">{e(local_tz)}</span>. Shaded areas are approximate sleep windows.</div></div>
<h2>Sleep <span class="dim">(approximate: motion + HR heuristic, not WHOOP sleep staging)</span></h2>
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
    el.textContent = `approx. sleep ${{f(+a, "hm")}}–${{f(+b, "hm")}}`;
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
        r = build_report(args.db, args.hours, args.address, demo=args.demo)
        out.write_text(render_html(r, args.refresh if args.watch else None))
        print(f"wrote {out}  ({len(r.samples)} samples, {len(r.sleep)} sleep window(s))", flush=True)
        if not args.watch:
            return
        time.sleep(args.refresh)


# ------------------------------------------------------------------------------------------ sync
def run_sync(quiet: bool = False) -> bool:
    """Reuse linux-capture: prefer the `whoop-sync` wrapper, else call whoop_sync.py directly."""
    cmd = ["whoop-sync", "sync"] if shutil.which("whoop-sync") else None
    if not cmd:
        lc = Path(os.environ.get("WHOOP_CAPTURE_DIR", "~/src/noop/tools/linux-capture")).expanduser()
        addr = os.environ.get("WHOOP_ADDRESS")
        if not addr:
            print("set WHOOP_ADDRESS (strap MAC) or install the whoop-sync wrapper", file=sys.stderr)
            return False
        cmd = [str(lc / ".venv/bin/python"), str(lc / "whoop_sync.py"), "sync", "--model", "whoop4",
               "--address", addr, "--subject", "me", "--db", str(DEFAULT_DB), "--max", "1800"]
    out = subprocess.DEVNULL if quiet else None
    return subprocess.run(cmd, stdout=out, stderr=out).returncode == 0


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="whoop-poc", description=__doc__.splitlines()[0])
    p.add_argument("cmd", nargs="?", default="tui", choices=["tui", "summary", "html"])
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"NOOP whoop.db (default {DEFAULT_DB}; env WHOOP_DB)")
    p.add_argument("--address", help="strap MAC if the DB holds several devices")
    p.add_argument("--hours", type=float, default=24, help="look-back window (default 24)")
    p.add_argument("--sync", action="store_true", help="drain new history from the strap first")
    p.add_argument("--refresh", type=int, default=30, help="TUI reload / html --watch interval, seconds")
    p.add_argument("--out", default="out/whoop.html", help="html output path (default out/whoop.html)")
    p.add_argument("--watch", action="store_true", help="html: regenerate every --refresh seconds")
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
        for _, line in text_lines(build_report(a.db, a.hours, a.address, demo=a.demo), width):
            print(line)
    else:
        run_html(a)


if __name__ == "__main__":
    main()

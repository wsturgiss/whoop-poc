#!/usr/bin/env python3
"""One night from the local NOOP whoop.db (read-only): NOOP session vs HDCZA in-bed window.

Usage: python3 -I bench/tib_night.py --since "2026-11-04 12:00" --until "2026-11-05 12:00"
                [--db PATH] [--v2] [--bed HH:MM] [--up HH:MM]
Times are local. --bed/--up are optional self-reported ground truth for the comparison lines.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import hdcza                                    # noqa: E402
from whoop_poc import DEFAULT_DB, STAGER_BIN    # noqa: E402


def local(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "-"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--v2", action="store_true")
    ap.add_argument("--bed", help="self-reported bedtime HH:MM (evening of --since)")
    ap.add_argument("--up", help="self-reported out-of-bed / wake HH:MM (morning of --until)")
    a = ap.parse_args()
    since = int(datetime.strptime(a.since, "%Y-%m-%d %H:%M").timestamp())
    until = int(datetime.strptime(a.until, "%Y-%m-%d %H:%M").timestamp())
    conn = sqlite3.connect(f"file:{a.db.expanduser()}?mode=ro", uri=True)
    sec = conn.execute("SELECT unix,hr,gx,gy,gz FROM feat_second WHERE unix BETWEEN ? AND ? ORDER BY unix",
                       (since, until)).fetchall()
    rr = conn.execute("SELECT unix,rr_ms FROM feat_rr WHERE unix BETWEEN ? AND ? ORDER BY unix,idx",
                      (since, until)).fetchall()
    conn.close()
    grav = [(u, x, y, z) for u, _, x, y, z in sec if x is not None]
    payload = {"hr": [{"ts": u, "bpm": h} for u, h, *_ in sec if h],
               "gravity": [{"ts": u, "x": x, "y": y, "z": z} for u, x, y, z in grav],
               "rr": [{"ts": u, "rrMs": v} for u, v in rr], "resp": [],
               "tzOffsetSeconds": int(datetime.fromtimestamp(until).astimezone().utcoffset().total_seconds()),
               "useSleepStagerV2": a.v2}
    res = subprocess.run([str(STAGER_BIN)], input=json.dumps(payload), capture_output=True, text=True, check=True)
    sessions = json.loads(res.stdout)
    w = hdcza.detect(grav)
    sess = max(sessions, key=lambda s: s["end"] - s["start"], default=None)
    b = hdcza.in_bed_bands(w, sess)
    print(f"data          {local(sec[0][0])} → {local(sec[-1][0])}  ({len(sec)} s, {len(rr)} RR)")
    print(f"NOOP session  {local(sess and sess['start'])} → {local(sess and sess['end'])}")
    print(f"HDCZA window  {local(w and w.start)} → {local(w and w.end)}  (threshold {w and round(w.threshold, 3)}°)")
    print(f"TIB (hull)    {local(b.get('tib_start'))} → {local(b.get('tib_end'))}  "
          f"onset {local(b.get('onset'))}  final wake {local(b.get('final_wake'))}")
    print(f"SOL {b.get('sol_min')} min · lie-in {b.get('lie_in_min')} min · WASO {b.get('waso_min')} min · "
          f"TST {b.get('tst_min')} min · TIB {b.get('tib_min')} min · SE {b.get('se') and round(b['se'], 3)}")
    for label, hhmm, mark in (("bed", a.bed, b.get("tib_start")), ("up", a.up, b.get("tib_end"))):
        if hhmm and mark:
            day = datetime.fromtimestamp(since if label == "bed" else until)
            h, m = map(int, hhmm.split(":"))
            ref = day.replace(hour=h, minute=m, second=0)
            if label == "bed" and ref.timestamp() < since:
                ref += timedelta(days=1)
            print(f"  TIB {label:<3} vs self-report {hhmm}: {round((mark - ref.timestamp()) / 60, 1):+} min")
    print("bands:")
    for x in b.get("bands", []):
        print(f"  {local(x['start'])}–{local(x['end'])}  {x['stage']}")


if __name__ == "__main__":
    main()

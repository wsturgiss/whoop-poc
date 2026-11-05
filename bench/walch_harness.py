#!/usr/bin/env python3
"""Score NOOP SleepStager (V1/V2, via stager/whoop-stage) against PSG on Walch et al. 2019.

Dataset: PhysioNet "Motion and heart rate from a wrist-worn wearable and labeled sleep from
polysomnography" 1.0.0 (ODC-By 1.0). Download + unzip it OUTSIDE this repo, e.g. ~/data/sleep-accel/,
and point WALCH_DIR (or --data) at the folder holding motion/ heart_rate/ labels/.

What it does per subject:
  Apple Watch accel (~50 Hz, g)  → 1 s mean = gravity vector (what WHOOP's decode gives us)
  Apple Watch PPG HR (sparse)    → HR samples as-is, or forward-filled to 1 Hz (--hr-fill)
  no RR / resp in this dataset   → RMSSD / RSA / resp paths in the stager get nothing
  timestamps are seconds since PSG start; we anchor PSG t=0 at a NOMINAL 23:00 local (tz 0)
  because the stager's daytime/overnight guards look at local clock hour.
Then runs whoop-stage, maps its sessions/segments onto the 30 s PSG epochs (outside any session =
wake) and reports epoch agreement + kappa (2- and 4-class) and onset / final-wake error in minutes.

Usage: python3 bench/walch_harness.py [--data DIR] [--v2] [--hr-fill SECONDS] [--json OUT]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGER = Path(os.environ.get("WHOOP_STAGE", ROOT / "stager/.build/release/whoop-stage"))
BASE = 1577919600          # 2020-01-01 23:00:00 UTC: nominal lights-off anchor for PSG t=0
EPOCH = 30
PSG4 = {0: "wake", 1: "light", 2: "light", 3: "deep", 4: "deep", 5: "rem"}
CLASSES4 = ["wake", "light", "deep", "rem"]


def find_root(d: Path) -> Path:
    for p in [d, *d.rglob("*")]:
        if p.is_dir() and (p / "labels").is_dir() and (p / "motion").is_dir():
            return p
    raise SystemExit(f"no motion/ + labels/ under {d} (download + unzip sleep-accel 1.0.0 there)")


def read_rows(path: Path) -> list[list[float]]:
    rows = []
    with open(path) as f:
        for line in f:
            parts = line.replace(",", " ").split()
            if parts:
                try:
                    rows.append([float(x) for x in parts])
                except ValueError:
                    pass
    return rows


def load_subject(root: Path, sid: str, hr_fill: int):
    labels = [(int(t), int(l)) for t, l in read_rows(root / "labels" / f"{sid}_labeled_sleep.txt")]
    acc = read_rows(root / "motion" / f"{sid}_acceleration.txt")
    hr = read_rows(root / "heart_rate" / f"{sid}_heartrate.txt")
    # gravity: per-second mean of raw accel (low-pass), magnitude left as-is (≈1 g when still)
    sums: dict[int, list[float]] = {}
    for t, x, y, z in (r[:4] for r in acc if len(r) >= 4):
        s = sums.setdefault(int(t // 1), [0.0, 0.0, 0.0, 0])
        s[0] += x; s[1] += y; s[2] += z; s[3] += 1
    grav = [{"ts": BASE + t, "x": s[0] / s[3], "y": s[1] / s[3], "z": s[2] / s[3]}
            for t, s in sorted(sums.items())]
    lo, hi = min(sums) - 3600, max(sums) + 3600         # HR only around the accel recording
    pts = sorted((int(t), int(round(b))) for t, b in (r[:2] for r in hr if len(r) >= 2)
                 if lo <= t <= hi and 25 <= b <= 220)
    if hr_fill:   # forward-fill to 1 Hz across gaps up to hr_fill seconds
        filled = []
        for (t, b), nxt in zip(pts, pts[1:] + [(pts[-1][0] + 1, 0)] if pts else []):
            for u in range(t, min(nxt[0], t + hr_fill)):
                filled.append((u, b))
        pts = filled
    seen, hrs = set(), []
    for t, b in pts:
        if t not in seen:
            seen.add(t); hrs.append({"ts": BASE + t, "bpm": b})
    return labels, grav, hrs


def run_stager(grav, hrs, v2: bool):
    payload = {"hr": hrs, "gravity": grav, "rr": [], "resp": [], "tzOffsetSeconds": 0,
               "useSleepStagerV2": v2}
    res = subprocess.run([str(STAGER)], input=json.dumps(payload), capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:300])
    return json.loads(res.stdout)


def predict(sessions, t_abs: int) -> str:
    for s in sessions:
        if s["start"] <= t_abs < s["end"]:
            for g in s["stages"]:
                if g["start"] <= t_abs < g["end"]:
                    return g["stage"]
            return "light"          # inside a session but no segment: count as sleep
    return "wake"


def kappa(pairs, classes) -> float:
    n = len(pairs)
    if not n:
        return float("nan")
    po = sum(a == b for a, b in pairs) / n
    pe = sum((sum(a == c for a, _ in pairs) / n) * (sum(b == c for _, b in pairs) / n) for c in classes)
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")


def first_last_sleep(seq):
    """(onset_t, final_wake_t) in PSG seconds from [(t, stage)], or (None, None)."""
    sl = [t for t, s in seq if s != "wake"]
    return (sl[0], sl[-1] + EPOCH) if sl else (None, None)


def score(labels, sessions):
    ep = [(t, PSG4[l], predict(sessions, BASE + t + EPOCH // 2)) for t, l in labels if l in PSG4]
    if not ep:
        return None
    pairs4 = [(a, b) for _, a, b in ep]
    pairs2 = [(a != "wake", b != "wake") for a, b in pairs4]
    t0, t1 = ep[0][0], ep[-1][0] + EPOCH
    psg_on, psg_off = first_last_sleep([(t, a) for t, a, _ in ep])
    pr_on, pr_off = first_last_sleep([(t, b) for t, _, b in ep])
    ps = sum(a for a, _ in pairs2)
    pw = len(pairs2) - ps
    tst_psg, tst_pred = ps * EPOCH / 60, sum(b for _, b in pairs2) * EPOCH / 60
    # session that overlaps the PSG window the most (the stager's "in-bed" span)
    best = max(sessions, key=lambda s: max(0, min(s["end"], BASE + t1) - max(s["start"], BASE + t0)),
               default=None)
    m = lambda x: None if x is None else round(x / 60, 1)
    trail = 0
    for _, a, _ in reversed(ep):
        if a != "wake":
            break
        trail += EPOCH
    return {
        # PSG often stops while the subject is still asleep: wake error is then censored (≈0 by
        # construction). Only nights with real trailing wake test "awake in bed after waking".
        "psg_trailing_wake_min": m(trail),
        "wake_err_uncensored_min": m(pr_off - psg_off) if trail >= 5 * 60 and None not in (pr_off, psg_off) else None,
        "epochs": len(ep),
        "acc2": sum(a == b for a, b in pairs2) / len(pairs2), "kappa2": kappa(pairs2, [True, False]),
        "acc4": sum(a == b for a, b in pairs4) / len(pairs4), "kappa4": kappa(pairs4, CLASSES4),
        "sens_sleep": sum(a and b for a, b in pairs2) / ps if ps else float("nan"),
        "spec_wake": sum((not a) and (not b) for a, b in pairs2) / pw if pw else float("nan"),
        "psg_sol_min": m(psg_on - t0) if psg_on is not None else None,
        "onset_err_min": m(pr_on - psg_on) if None not in (pr_on, psg_on) else None,
        "wake_err_min": m(pr_off - psg_off) if None not in (pr_off, psg_off) else None,
        "session_start_vs_lights_off_min": m(best["start"] - BASE - t0) if best else None,
        "session_end_vs_psg_end_min": m(best["end"] - BASE - t1) if best else None,
        "tst_psg_min": round(tst_psg, 1), "tst_pred_min": round(tst_pred, 1),
        "se_psg": ps / len(pairs2), "se_pred": sum(b for _, b in pairs2) / len(pairs2),
        "n_sessions": len(sessions),
        "pairs4": pairs4,
    }


def summarize(name, rows):
    ok = [r for r in rows if r.get("acc2") is not None]
    def agg(k, absval=False):
        v = [abs(r[k]) if absval else r[k] for r in ok if r.get(k) is not None]
        return (st.median(v), st.mean(v), len(v)) if v else (float("nan"),) * 2 + (0,)
    pooled = [p for r in ok for p in r["pairs4"]]
    conf = {a: {b: 0 for b in CLASSES4} for a in CLASSES4}
    for a, b in pooled:
        conf[a][b] += 1
    out = {"variant": name, "subjects": len(ok),
           "pooled_acc4": sum(a == b for a, b in pooled) / len(pooled),
           "pooled_kappa4": kappa(pooled, CLASSES4),
           "pooled_acc2": sum((a != "wake") == (b != "wake") for a, b in pooled) / len(pooled),
           "pooled_kappa2": kappa([(a != "wake", b != "wake") for a, b in pooled], [True, False]),
           "confusion_psg_rows_pred_cols": conf}
    for k in ("acc2", "kappa2", "acc4", "kappa4", "sens_sleep", "spec_wake", "se_psg", "se_pred",
              "psg_sol_min", "session_start_vs_lights_off_min", "session_end_vs_psg_end_min"):
        out[k] = agg(k)
    for k in ("onset_err_min", "wake_err_min", "wake_err_uncensored_min"):
        out[k] = agg(k)
        out["abs_" + k] = agg(k, absval=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("WALCH_DIR", "~/data/sleep-accel")).expanduser())
    ap.add_argument("--v2", action="store_true", help="useSleepStagerV2 (V1 session detection, V2 staging)")
    ap.add_argument("--hr-fill", type=int, default=0, help="forward-fill HR to 1 Hz across gaps ≤ N s (0 = raw)")
    ap.add_argument("--json", type=Path, help="write per-subject + summary JSON here (keep it out of git)")
    ap.add_argument("--only", nargs="*", help="subject ids")
    a = ap.parse_args()
    if not STAGER.exists():
        raise SystemExit(f"missing {STAGER}: run stager/build.sh")
    root = find_root(a.data)
    sids = a.only or sorted(p.name.split("_")[0] for p in (root / "labels").glob("*_labeled_sleep.txt"))
    name = f"{'V2' if a.v2 else 'V1'}{f' hr-fill={a.hr_fill}s' if a.hr_fill else ''}"
    rows = []
    print(f"{'subject':>9} {'ep':>4} {'acc2':>5} {'k2':>5} {'acc4':>5} {'k4':>5} {'onsetΔ':>7} {'wakeΔ':>7} "
          f"{'sessΔ':>6} {'TSTpsg':>6} {'TSTprd':>6} n", file=sys.stderr)
    for sid in sids:
        try:
            labels, grav, hrs = load_subject(root, sid, a.hr_fill)
            r = score(labels, run_stager(grav, hrs, a.v2))
        except Exception as e:      # one bad subject shouldn't sink the run
            print(f"{sid:>9} ERROR {e}", file=sys.stderr)
            continue
        if not r:
            continue
        r["subject"] = sid
        rows.append(r)
        f = lambda x, w=7: f"{x:>{w}}" if x is not None else f"{'-':>{w}}"
        print(f"{sid:>9} {r['epochs']:>4} {r['acc2']:.2f}  {r['kappa2']:.2f}  {r['acc4']:.2f}  {r['kappa4']:.2f} "
              f"{f(r['onset_err_min'])} {f(r['wake_err_min'])} {f(r['session_start_vs_lights_off_min'], 6)} "
              f"{r['tst_psg_min']:>6} {r['tst_pred_min']:>6} {r['n_sessions']}", file=sys.stderr)
    s = summarize(name, rows)
    print(json.dumps({k: v for k, v in s.items() if k != "confusion_psg_rows_pred_cols"}, indent=1, default=str))
    print("confusion (PSG rows → pred cols):", json.dumps(s["confusion_psg_rows_pred_cols"]))
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"summary": s, "subjects": [{k: v for k, v in r.items() if k != "pairs4"}
                                                                 for r in rows]}, indent=1, default=str))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Score the HDCZA in-bed (TIB) window, alone and combined with NOOP sleep, against PSG.

Datasets (download outside the repo):
  walch      PhysioNet sleep-accel 1.0.0 (ODC-By), WALCH_DIR, default ~/data/sleep-accel
             Apple Watch accel + HR. HDCZA edges + combined TIB/NOOP metrics.
  newcastle  Zenodo 1160410 (CC BY 4.0), NEWCASTLE_DIR, default ~/data/newcastle-psg
             GENEActiv raw accel on both wrists, no HR. HDCZA edges only.

PSG reference per night: lights-off = first scored epoch, lights-on = end of the last scored
epoch, onset = first non-wake epoch, final wake = end of the last non-wake epoch.

Usage: python3 -I bench/tib_harness.py walch|newcastle [--v2] [--json OUT] [--only IDS...]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "bench")]
import hdcza                                    # noqa: E402
import walch_harness as wh                      # noqa: E402

EPOCH = 30
LOOKBACK_S = 12 * 3600       # HDCZA input = [lights-off − 12 h, lights-off + 14 h] ≈ GGIR's noon→noon day
LOOKAHEAD_S = 14 * 3600


def psg_marks(labels):
    """labels [(t, stage4)] sorted → lights_off, lights_on, onset, final_wake (same clock as t)."""
    sl = [t for t, s in labels if s != "wake"]
    return (labels[0][0], labels[-1][0] + EPOCH,
            sl[0] if sl else None, sl[-1] + EPOCH if sl else None)


def mins(a, b):
    return None if a is None or b is None else round((a - b) / 60, 1)


# ------------------------------------------------------------------------- walch

def walch_nights(only):
    root = wh.find_root(Path(os.environ.get("WALCH_DIR", "~/data/sleep-accel")).expanduser())
    sids = only or sorted(p.name.split("_")[0] for p in (root / "labels").glob("*_labeled_sleep.txt"))
    for sid in sids:
        labels, grav, hrs = wh.load_subject(root, sid, 0)
        lab = [(wh.BASE + t, wh.PSG4[l]) for t, l in labels if l in wh.PSG4]
        lo, hi = lab[0][0] - LOOKBACK_S, lab[0][0] + LOOKAHEAD_S
        gp = [p for p in grav if lo <= p["ts"] < hi]
        yield (sid, lab, [(p["ts"], p["x"], p["y"], p["z"]) for p in gp], gp,
               [h for h in hrs if lo <= h["ts"] < hi])


def score_walch(sid, lab, g, grav_payload, hrs, v2):
    off, on, onset, final = psg_marks(lab)
    w = hdcza.detect(g)
    sessions = wh.run_stager(grav_payload, hrs, v2)
    sess = max(sessions, key=lambda s: max(0, min(s["end"], on) - max(s["start"], off)), default=None)
    if sess and min(sess["end"], on) <= max(sess["start"], off):
        sess = None
    b = hdcza.in_bed_bands(w, sess)
    # NOOP alone: TIB = its session
    n = hdcza.in_bed_bands(None, sess) if sess else {}
    psg_tst = sum(EPOCH for _, s in lab if s != "wake")
    trail = (on - final) if final else None
    r = {
        "subject": sid,
        "hdcza_threshold": round(w.threshold, 3) if w else None,
        "hdcza_start_vs_lights_off": mins(w.start, off) if w else None,
        "hdcza_start_vs_psg_onset": mins(w.start, onset) if w else None,
        "hdcza_end_vs_psg_final_wake": mins(w.end, final) if w else None,
        "hdcza_end_vs_lights_on": mins(w.end, on) if w else None,
        "noop_start_vs_lights_off": mins(sess["start"], off) if sess else None,
        "tib_start_vs_lights_off": mins(b.get("tib_start"), off),
        "tib_end_vs_lights_on": mins(b.get("tib_end"), on),
        "psg_sol": mins(onset, off), "noop_sol": n.get("sol_min"), "tib_sol": b.get("sol_min"),
        "psg_trailing_wake": mins(trail, 0) if trail is not None else None,
        "noop_lie_in": n.get("lie_in_min"), "tib_lie_in": b.get("lie_in_min"),
        "psg_se": psg_tst / (on - off),
        "noop_se": n.get("se"), "tib_se": b.get("se"),
    }
    # epoch view: does "inbed_wake" cover PSG wake epochs that NOOP alone called out-of-session?
    def at(bands, t):
        for x in bands:
            if x["start"] <= t < x["end"]:
                return x["stage"]
        return "out"
    cnt = {"psg_wake": 0, "psg_wake_inbed_wake": 0, "psg_wake_out": 0, "psg_wake_called_sleep": 0}
    for t, s in lab:
        if s == "wake":
            p = at(b.get("bands", []), t + EPOCH // 2)
            cnt["psg_wake"] += 1
            cnt["psg_wake_inbed_wake" if p in ("inbed_wake", "wake") else
                "psg_wake_out" if p == "out" else "psg_wake_called_sleep"] += 1
    r.update(cnt)
    return r


# ------------------------------------------------------------------------- newcastle

def read_geneactiv(path: Path, decimate: int = 1):
    """GENEActiv .bin (hex text pages) → 1 Hz mean gravity [(ts, x, y, z)] (timestamps as written,
    treated as local clock = UTC here) plus the first page time. 12-bit signed x/y/z per sample."""
    import datetime as dt
    gains, offsets = {}, {}
    out: dict[int, list[float]] = {}
    with open(path, "r", errors="replace") as f:
        lines = f.read().splitlines()
    i = 0
    while i < len(lines) and lines[i].strip() != "Recorded Data":
        ln = lines[i]
        for ax in "xyz":
            if ln.startswith(f"{ax} gain:"):
                gains[ax] = float(ln.split(":", 1)[1])
            elif ln.startswith(f"{ax} offset:"):
                offsets[ax] = float(ln.split(":", 1)[1])
        i += 1
    while i < len(lines):
        if lines[i].strip() != "Recorded Data":
            i += 1
            continue
        hdr = lines[i + 1:i + 9]
        data = lines[i + 9].strip() if i + 9 < len(lines) else ""
        i += 10
        info = dict(h.split(":", 1) for h in hdr if ":" in h)
        t_str = info.get("Page Time", "").strip()
        freq = float(info.get("Measurement Frequency", "0").strip().replace(",", ".") or 0)
        if not t_str or not freq or not data:
            continue
        # "2013-06-10 19:30:00:500": clock time, then milliseconds after the last colon
        base = dt.datetime.strptime(t_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        t0 = base.timestamp() + int(t_str[20:23] or 0) / 1000
        nsamp = len(data) // 12
        for k in range(0, nsamp, decimate):
            v = int(data[k * 12:(k + 1) * 12], 16)
            raw = []
            for shift in (36, 24, 12):
                c = (v >> shift) & 0xFFF
                raw.append(c - 4096 if c & 0x800 else c)
            ts = int(t0 + k / freq)
            s = out.setdefault(ts, [0.0, 0.0, 0.0, 0])
            for j, ax in enumerate("xyz"):
                s[j] += (raw[j] * 100 - offsets[ax]) / gains[ax]
            s[3] += 1
    return [(t, s[0] / s[3], s[1] / s[3], s[2] / s[3]) for t, s in sorted(out.items())]


def read_remlogic(path: Path):
    """RemLogic sleep-stage export → [(ts, stage4)] on the same naive-clock-as-UTC basis."""
    import datetime as dt
    date, rows, prev, day = None, [], None, 0
    for ln in path.read_text(errors="replace").splitlines():
        f = [c.strip() for c in ln.split("\t")]
        if ln.startswith("Recording Date:"):
            date = dt.datetime.strptime(ln.split(":", 1)[1].strip(), "%d/%m/%Y")
        elif len(f) >= 4 and f[0] in NCL_STAGE and date:
            clock = next(c for c in f[1:] if c.count(":") == 2)   # some exports omit "Position"
            hh, mm, ss = map(int, clock.split(":"))
            sec = hh * 3600 + mm * 60 + ss
            if prev is not None and sec < prev:
                day += 1
            prev = sec
            t = (date + dt.timedelta(days=day, seconds=sec)).replace(tzinfo=dt.timezone.utc).timestamp()
            if NCL_STAGE[f[0]]:
                rows.append((int(t), NCL_STAGE[f[0]]))
    return rows


NCL_STAGE = {"W": "wake", "N1": "light", "N2": "light", "N3": "deep", "R": "rem", "Unscored": None}


def run_newcastle(only, wrist: str, pre_min: int = 120):
    root = Path(os.environ.get("NEWCASTLE_DIR", "~/data/newcastle-psg")).expanduser()
    psg = {p.name.split("_")[0][len("mecsleep"):]: p for p in root.rglob("mecsleep*_psg.txt")}
    acc = {}
    for p in root.rglob("MECSLEEP*.bin"):
        sid, side = p.name.split("_")[0][len("MECSLEEP"):], p.name.split("_")[1].split()[0]
        acc[(sid, side)] = p
    rows = []
    for sid in sorted(only or psg):
        b = acc.get((sid, wrist))
        if not b or sid not in psg:
            print(f"{sid} skip: no {wrist}-wrist file", file=sys.stderr)
            continue
        lab = read_remlogic(psg[sid])
        grav = read_geneactiv(b, decimate=4)
        # Some PSG "Recording Date" headers are a day off; shift ±24 h until PSG overlaps the
        # accel recording (same rule as van Hees' psg-ncl-acc-spt-detection-eval, Apache-2.0).
        shift = 0
        if grav and lab:
            if lab[0][0] - grav[0][0] > 12 * 3600:
                shift = -86400
            elif lab[0][0] - grav[0][0] < -12 * 3600:
                shift = 86400
        lab = [(t + shift, s) for t, s in lab]
        rec0, rec1, onset, final = psg_marks(lab)
        # Devices kept recording for hours after the study, often off-wrist and perfectly still,
        # which HDCZA reads as "no movement". Without a non-wear mask, bound the input to
        # [PSG start − pre_min, PSG end] (van Hees' eval used only the PSG overlap).
        g = [x for x in grav if rec0 - pre_min * 60 <= x[0] < rec1]
        w = hdcza.detect(g)
        r = {"subject": sid, "wrist": wrist, "psg_day_shift_h": shift // 3600, "hdcza_threshold": round(w.threshold, 3) if w else None,
             "psg_sleep_period_h": round((final - onset) / 3600, 2) if onset else None,
             "hdcza_start_vs_psg_onset": mins(w.start, onset) if w else None,
             "hdcza_end_vs_psg_final_wake": mins(w.end, final) if w else None,
             "hdcza_start_vs_recording_start": mins(w.start, rec0) if w else None,
             "psg_onset_vs_recording_start": mins(onset, rec0),
             # accel recordings were configured for 12 h; < 0 means the device stopped before final wake
             "acc_end_vs_psg_final_wake": mins(grav[-1][0], final) if grav and final else None}
        if r["acc_end_vs_psg_final_wake"] is not None and r["acc_end_vs_psg_final_wake"] < 5:
            r["hdcza_end_vs_psg_final_wake"] = None          # censored: device ended first
        # within the HDCZA window: share of PSG-wake epochs (awake in bed by this definition)
        if w:
            inside = [s for t, s in lab if w.start <= t < w.end]
            r["psg_wake_frac_inside_hdcza"] = round(sum(s == "wake" for s in inside) / len(inside), 3) if inside else None
        print(f"{sid} {wrist}: start Δonset {r['hdcza_start_vs_psg_onset']} min, "
              f"end Δfinal-wake {r['hdcza_end_vs_psg_final_wake']} min", file=sys.stderr)
        rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dataset", choices=["walch", "newcastle"])
    ap.add_argument("--v2", action="store_true")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--wrist", choices=["left", "right"], default="left", help="newcastle: which wrist")
    ap.add_argument("--pre-min", type=int, default=120, help="newcastle: accel minutes kept before PSG start")
    a = ap.parse_args()
    rows = []
    if a.dataset == "walch":
        for sid, lab, g, gp, hrs in walch_nights(a.only):
            try:
                rows.append(score_walch(sid, lab, g, gp, hrs, a.v2))
            except Exception as e:
                print(f"{sid} ERROR {e}", file=sys.stderr)
    else:
        rows = run_newcastle(a.only, a.wrist, a.pre_min)
    summary = summarize(rows)
    print(json.dumps(summary, indent=1))
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"summary": summary, "subjects": rows}, indent=1))


def summarize(rows):
    out = {"n": len(rows)}
    keys = [k for k in (rows[0] if rows else {}) if k != "subject"]
    for k in keys:
        v = [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        if v:
            out[k] = {"median": round(st.median(v), 3), "mean": round(st.mean(v), 3),
                      "mae": round(st.mean(abs(x) for x in v), 3), "n": len(v)}
    return out


if __name__ == "__main__":
    main()

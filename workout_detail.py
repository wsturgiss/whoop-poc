"""Computed per-workout detail for Coach (`whoop-poc coach workout <id>` → whoop-poc/workout@2).

Pure functions over per-second samples (`w.Sample`, 1 Hz HR and gravity from type-47 history) and,
where present, 100 Hz IMU packets (type-43 REALTIME_RAW_DATA, which the strap streams only during a
live session). Definitions of every field are in COACH.md, "Workout detail".
"""
from __future__ import annotations

import sqlite3
import statistics as st
import struct
from pathlib import Path

import whoop_poc as w

PEAK_WINDOW_S = 5          # peak_hr_5s: highest mean over this many consecutive seconds ...
PEAK_MIN_VALID = 4         # ... with at least this many HR readings in the window
HR_RANGE = (30, 230)       # clean_hr: readings outside this are sensor errors
SPIKE_BPM = 10             # ... a reading this far from every reading within SPIKE_NEAR_S of it is a spike
SPIKE_NEAR_S = 2
PEAK_CONTEXT_S = 30        # clean_peak: the 5 s mean must be within PEAK_CONTEXT_BPM of the mean over
PEAK_CONTEXT_BPM = 15      # ... PEAK_CONTEXT_S either side (a short burst of noise is not a peak) ...
PEAK_CONTEXT_MIN_VALID = 30   # ... which needs at least this many readings
BIN_S = 10                 # HR bins for intervals, drift and the 10 s series
BIN_MIN_VALID = 5          # a bin needs this many HR readings
RECOVERY_PROBE_S = 5       # HR at a moment = mean over this many seconds (ending at effort end; centred after)
RECOVERY_MIN_VALID = 3     # ... with at least this many readings, else the drop is null
WARMUP_S = 5 * 60          # drift ignores the first 5 min (HR still climbing)
STEADY_MIN_CORE_S = 10 * 60   # drift needs at least this much effort after the warm-up
STEADY_MAX_RESID_BPM = 6.0    # steady = 30 s means stay within this RMS of a straight line
INTERVAL_BIN_S = 30        # intervals: 30 s HR means ...
SWING_BPM = 15             # ... where a reversal of at least this much separates segments
MIN_REST_S = 30            # ... a rest must fall for at least this long
MIN_RESTS = 2              # ... and there must be this many rests inside the effort
SET_MOTION = 0.15          # lifting: 5 s mean per-second |Δgravity| (g, L1) at or above this = moving
SET_MIN_S = 10             # a set lasts at least this long
SET_MERGE_S = 8            # pauses shorter than this stay inside one set
IMU_TYPE = 43              # REALTIME_RAW_DATA
IMU_DATA_LEN = 1917        # payload length of the IMU variant (1921 is optical)
IMU_AXES = (89, 289, 489)  # accel X/Y/Z: 100 signed int16 LE each, 1/4096 g per LSB
IMU_RATE_HZ = 100
CADENCE_WINDOW_S = 10      # cadence: one estimate per window of IMU packets
CADENCE_SPM = (100, 220)   # step rates searched (walking to fast running)
CADENCE_MIN_CORR = 0.3     # autocorrelation peak needed to accept a window
CADENCE_MIN_S = 60         # cadence needs this much IMU coverage of the effort


def hr_seconds(samples: list[w.Sample], a: int, b: int) -> dict[int, int]:
    """{unix: bpm} for worn seconds in [a, b)."""
    return {s.unix: s.hr for s in samples if a <= s.unix < b and s.hr > 0}


def mean_over(hr: dict[int, int], a: int, b: int, min_valid: int) -> float | None:
    v = [hr[t] for t in range(a, b) if t in hr]
    return st.mean(v) if len(v) >= min_valid else None


def peak_5s(hr: dict[int, int]) -> int | None:
    """Highest mean HR over PEAK_WINDOW_S consecutive seconds (windows with < PEAK_MIN_VALID readings skipped)."""
    best = None
    for t in hr:
        m = mean_over(hr, t, t + PEAK_WINDOW_S, PEAK_MIN_VALID)
        if m is not None and (best is None or m > best):
            best = m
    return None if best is None else round(best)


def clean_hr(hr: dict[int, int]) -> dict[int, int]:
    """hr without readings outside HR_RANGE and without spikes: a reading more than SPIKE_BPM from every
    reading within SPIKE_NEAR_S seconds of it. A reading with no neighbours is kept."""
    out = {}
    for t, v in hr.items():
        if not HR_RANGE[0] <= v <= HR_RANGE[1]:
            continue
        near = [hr[u] for u in range(t - SPIKE_NEAR_S, t + SPIKE_NEAR_S + 1) if u != t and u in hr]
        if near and all(abs(v - x) > SPIKE_BPM for x in near):
            continue
        out[t] = v
    return out


def clean_peak(hr: dict[int, int]) -> tuple[int, int] | None:
    """(bpm, window start unix) of the highest clean 5 s peak: peak_5s's window mean over clean_hr
    readings, kept only where it is within PEAK_CONTEXT_BPM of the mean over PEAK_CONTEXT_S either side."""
    c = clean_hr(hr)
    best = None
    for t in c:
        m = mean_over(c, t, t + PEAK_WINDOW_S, PEAK_MIN_VALID)
        if m is None or (best is not None and m <= best[0]):
            continue
        ctx = mean_over(c, t - PEAK_CONTEXT_S, t + PEAK_WINDOW_S + PEAK_CONTEXT_S, PEAK_CONTEXT_MIN_VALID)
        if ctx is not None and m - ctx <= PEAK_CONTEXT_BPM:
            best = (m, t)
    return None if best is None else (round(best[0]), best[1])


def bins(hr: dict[int, int], a: int, b: int, size: int = BIN_S) -> list[tuple[int, float]]:
    """[(bin_start, mean bpm)] for bins in [a, b) with at least BIN_MIN_VALID readings."""
    out = []
    for t in range(a, b, size):
        m = mean_over(hr, t, min(t + size, b), BIN_MIN_VALID)
        if m is not None:
            out.append((t, m))
    return out


def effort_end(hr: dict[int, int], start: int, end: int, avg_hr: float) -> int | None:
    """When the effort stopped: the top of the final HR descent. Take the last 10 s bin in [start, end + 60)
    whose mean is at or above the midpoint of Z1's low bound and the workout's avg HR, then the highest
    bin in the 60 s up to it (ties: the latest); the effort end is that bin's end. The detected end is
    later, because a minute only drops out of a workout once its mean falls below Z1."""
    thr = (w.zone_low(w.ZONES[-1]) + avg_hr) / 2
    bs = bins(hr, start, end + 60)
    hot = [t for t, m in bs if m >= thr]
    if not hot:
        return None
    top = max(((m, t) for t, m in bs if hot[-1] - 60 < t <= hot[-1]))
    return top[1] + BIN_S


def recovery(hr: dict[int, int], at: int | None) -> dict:
    """HR drop 1 and 2 min after the effort end; each null (with why) if the strap has no readings there."""
    if at is None:
        return {"drop_1min": None, "drop_2min": None, "why": "no effort end found"}
    h0 = mean_over(hr, at - RECOVERY_PROBE_S, at, RECOVERY_MIN_VALID)
    out = {"end_hr": None if h0 is None else round(h0)}
    why = []
    for k, dt in (("drop_1min", 60), ("drop_2min", 120)):
        h = mean_over(hr, at + dt - 2, at + dt + 3, RECOVERY_MIN_VALID)
        out[k] = None if h0 is None or h is None else round(h0 - h)
        if out[k] is None:
            why.append(f"{k}: fewer than {RECOVERY_MIN_VALID} HR readings at "
                       + ("the effort end" if h0 is None else f"+{dt // 60} min (strap gap)"))
    if why:
        out["why"] = "; ".join(why)
    return out


def zigzag(series: list[tuple[int, float]], swing: float = SWING_BPM) -> list[tuple[int, float]]:
    """Alternating HR extremes [(t, bpm)]: an extreme is confirmed once HR reverses from it by `swing`."""
    if not series:
        return []
    lo = hi = series[0]
    ext: list[tuple[int, float]] = []
    trend, cand = 0, None        # trend +1: rising toward cand (a peak); -1: falling toward cand (a trough)
    for p in series[1:]:
        if trend == 0:
            lo = p if p[1] <= lo[1] else lo        # ties move to the latest point: a plateau's extreme is its end
            hi = p if p[1] >= hi[1] else hi
            if p[1] - lo[1] >= swing:
                ext, trend, cand = [lo], 1, p
            elif hi[1] - p[1] >= swing:
                ext, trend, cand = [hi], -1, p
        elif (p[1] - cand[1]) * trend >= 0:
            cand = p
        elif abs(cand[1] - p[1]) >= swing:
            ext.append(cand)
            trend, cand = -trend, p
    return ext + [cand] if cand else []


def intervals(hr: dict[int, int], start: int, stop: int) -> list[dict]:
    """Work/rest segments from HR reversals of at least SWING_BPM in 30 s means over [start, stop).
    A rising leg is work, a falling leg is rest; HR lags effort by ~30 s, so segments do too. A rest
    counts when it falls for at least MIN_REST_S and a work leg follows it (the last descent is the
    cool-down). Empty unless at least MIN_RESTS rests count: a steady effort."""
    ext = zigzag(bins(hr, start, stop, INTERVAL_BIN_S))
    legs = [("work" if hb > ha else "rest", ta, tb, ha, hb) for (ta, ha), (tb, hb) in zip(ext, ext[1:])]
    rests = [g for g, nxt in zip(legs, legs[1:])
             if g[0] == "rest" and nxt[0] == "work" and g[2] - g[1] >= MIN_REST_S]
    if len(rests) < MIN_RESTS:
        return []
    out = []
    for kind, a, b, ha, hb in legs:
        seg_hr = {t: v for t, v in hr.items() if a <= t < b}
        if not seg_hr:
            continue
        seg = {"kind": kind, "start_unix": a, "sec": b - a, "avg_hr": round(st.mean(seg_hr.values())),
               "peak_hr_5s": peak_5s(seg_hr)}
        if kind == "rest":
            seg["hr_drop"] = round(ha - hb)
        out.append(seg)
    return out


def drift(hr: dict[int, int], start: int, stop: int, has_intervals: bool) -> dict:
    """Cardiac drift on a steady effort: avg HR of the second half minus the first half of [start +
    WARMUP_S, stop). Steady = no intervals, at least STEADY_MIN_CORE_S after the warm-up, and 30 s means
    within STEADY_MAX_RESID_BPM RMS of a straight line (a steady drift is a line; surges are not)."""
    a = start + WARMUP_S
    if has_intervals:
        return {"steady": False, "drift_bpm": None, "why": "intervals detected"}
    if stop - a < STEADY_MIN_CORE_S:
        return {"steady": False, "drift_bpm": None, "why": f"under {STEADY_MIN_CORE_S // 60} min after a {WARMUP_S // 60} min warm-up"}
    pts = bins(hr, a, stop, 30)
    if len(pts) < 4:
        return {"steady": False, "drift_bpm": None, "why": "too few HR readings"}
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    mx, my = st.mean(xs), st.mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    resid = (st.mean([(y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys)])) ** 0.5
    mid = (a + stop) // 2
    h1, h2 = mean_over(hr, a, mid, 1), mean_over(hr, mid, stop, 1)
    out = {"steady": resid <= STEADY_MAX_RESID_BPM, "resid_bpm": round(resid, 1),
           "from_unix": a, "to_unix": stop}
    if not out["steady"]:
        return {**out, "drift_bpm": None, "why": f"30 s HR strays {resid:.1f} bpm RMS from a line (> {STEADY_MAX_RESID_BPM:g})"}
    return {**out, "first_half_hr": round(h1), "second_half_hr": round(h2), "drift_bpm": round(h2 - h1, 1),
            "drift_pct": round(100 * (h2 - h1) / h1, 1)}


def z4_plus_min(hr: dict[int, int]) -> float:
    """Minutes (per-second readings / 60) at or above Z4's low bound."""
    lo = w.zone_low(w.ZONES[1])
    return round(sum(1 for v in hr.values() if v >= lo) / 60, 1)


def lift_sets(samples: list[w.Sample], a: int, b: int) -> dict:
    """Sets from wrist motion: runs where the 5 s mean per-second |Δgravity| is at least SET_MOTION, merged
    across pauses shorter than SET_MERGE_S, kept when at least SET_MIN_S long. 1 Hz gravity only:
    unvalidated, no labelled lifting sessions yet."""
    motion: dict[int, float] = {}
    prev = None
    for s in samples:
        if a <= s.unix < b and prev is not None and s.unix - prev.unix == 1:
            motion[s.unix] = abs(s.gx - prev.gx) + abs(s.gy - prev.gy) + abs(s.gz - prev.gz)
        prev = s
    if len(motion) < (b - a) // 2:
        return {"sets": None, "why": "gravity readings cover under half the workout"}
    runs: list[list[int]] = []
    for t in range(a, b):
        v = [motion[u] for u in range(t - 2, t + 3) if u in motion]
        if v and st.mean(v) >= SET_MOTION:
            if runs and t - runs[-1][1] < SET_MERGE_S:
                runs[-1][1] = t + 1
            else:
                runs.append([t, t + 1])
    sets = [r for r in runs if r[1] - r[0] >= SET_MIN_S]
    return {"sets": len(sets), "set_sec": [y - x for x, y in sets],
            "rest_sec": [n[0] - p[1] for p, n in zip(sets, sets[1:])],
            "total_min": round((sets[-1][1] - sets[0][0]) / 60, 1) if sets else 0,
            "method": "1 Hz gravity motion; unvalidated"}


# ------------------------------------------------------------------------------------------ IMU
def load_imu(db: Path, a: int, b: int) -> list[tuple[int, list[float]]]:
    """[(unix, 100 accel magnitudes in g)] from type-43 IMU packets with device time in [a, b). Read-only."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:   # type-43 rows have no unix column; recv time bounds the query, the packet's own time filters it
            rows = conn.execute("SELECT hex FROM frames WHERE inner_type=? AND recv_ms BETWEEN ? AND ?",
                                (IMU_TYPE, (a - 3600) * 1000, (b + 7 * 86400) * 1000)).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError:      # no DB or no frames table: no IMU
        return []
    out = {}
    for (h,) in rows:
        r = bytes.fromhex(h)
        if len(r) < IMU_AXES[-1] + 200 or struct.unpack_from("<H", r, 1)[0] - 7 != IMU_DATA_LEN:
            continue
        t = struct.unpack_from("<I", r, 11)[0]
        if a <= t < b:
            ax = [struct.unpack_from("<100h", r, o) for o in IMU_AXES]
            out[t] = [(x * x + y * y + z * z) ** 0.5 / 4096 for x, y, z in zip(*ax)]
    return sorted(out.items())


def cadence(imu: list[tuple[int, list[float]]]) -> dict:
    """Steps per minute from 100 Hz accel magnitude: per CADENCE_WINDOW_S window of consecutive packets,
    the autocorrelation peak over step periods in CADENCE_SPM (rejected below CADENCE_MIN_CORR or on the
    range edge); the median of accepted windows."""
    if len(imu) < CADENCE_MIN_S:
        return {"cadence_spm": None, "imu_sec": len(imu),
                "why": "no 100 Hz IMU for this workout: the strap streams it (type 43) only during a live session; "
                       "its history keeps 1 Hz gravity, too slow for steps (~2.5-3 Hz)"}
    lags = range(IMU_RATE_HZ * 60 // CADENCE_SPM[1], IMU_RATE_HZ * 60 // CADENCE_SPM[0] + 1)
    est = []
    i = 0
    while i + CADENCE_WINDOW_S <= len(imu):
        win = imu[i:i + CADENCE_WINDOW_S]
        i += CADENCE_WINDOW_S
        if win[-1][0] - win[0][0] != CADENCE_WINDOW_S - 1:
            continue
        x = [v for _, m in win for v in m]
        mu = st.mean(x)
        x = [v - mu for v in x]
        e = sum(v * v for v in x)
        if e <= 1e-6:
            continue
        r = {k: sum(x[j] * x[j + k] for j in range(len(x) - k)) / e for k in lags}
        k = max(r, key=r.get)
        if r[k] >= CADENCE_MIN_CORR and lags[0] < k < lags[-1]:     # a peak on the range edge is not a step rate
            est.append(60 * IMU_RATE_HZ / k)
    if not est:
        return {"cadence_spm": None, "imu_sec": len(imu), "why": "no periodic step signal in the IMU"}
    return {"cadence_spm": round(st.median(est)), "imu_sec": len(imu), "windows": len(est)}

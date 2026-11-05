"""In-bed window from wrist z-angle (HDCZA) + "awake in bed" bands around NOOP sleep.

HDCZA: van Hees et al. 2018, "Estimating sleep parameters using an accelerometer without sleep
diary", Sci Rep 8:12975, doi:10.1038/s41598-018-31266-z. Reimplemented from the paper and the
defaults of GGIR's HASPT() (Apache-2.0, github.com/wadpac/GGIR, R/HASPT.R + R/load_params.R):

  1. anglez = atan(z / sqrt(x² + y²)) in degrees, averaged per 5 s epoch
  2. x[i] = median |Δanglez| over a centred 5-min window (edges = 0, as GGIR's rollapply fill=0)
  3. threshold = 15 × 10th percentile of x, clamped to [0.13, 0.5] degrees
  4. no-movement = x < threshold; drop no-movement blocks ≤ 30 min
  5. fill movement gaps < 60 min between no-movement blocks
  6. the longest remaining no-movement block is the window

Input is the 1 Hz gravity vector (WHOOP decode, or low-passed raw accel). Gaps longer than
`max_gap_s` count as movement, so a strap-off stretch never becomes "in bed".

GGIR calls this window the sleep period time (SPT) window. It is defined by posture change,
not cardiac sleep, so it can include still, awake time either side of sleep. That is the
"in bed" (TIB) interval this module uses.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

EPOCH_S = 5
ROLL_MIN = 5
PCTL, MULT, TMIN, TMAX = 10, 15.0, 0.13, 0.50
MIN_BLOCK_MIN = 30
MAX_GAP_MIN = 60


@dataclass
class Window:
    start: int            # unix s, first epoch of the window
    end: int              # unix s, end of the last epoch
    threshold: float      # degrees
    epochs: int           # 5 s epochs analysed


def anglez_epochs(grav, epoch_s: int = EPOCH_S, max_gap_s: int = 600):
    """grav: iterable of (ts, x, y, z) sorted by ts → (t0, angles, valid) on a regular epoch grid.
    Missing epochs inside a gap ≤ max_gap_s carry the last angle forward; longer gaps are invalid."""
    sums: dict[int, list[float]] = {}
    for ts, x, y, z in grav:
        h = math.hypot(x, y)
        if h == 0 and z == 0:
            continue
        a = math.degrees(math.atan2(z, h))          # = atan(z / sqrt(x²+y²)) for h > 0
        e = int(ts) // epoch_s
        s = sums.setdefault(e, [0.0, 0])
        s[0] += a
        s[1] += 1
    if not sums:
        return 0, [], []
    e0, e1 = min(sums), max(sums)
    angles, valid = [], []
    last, last_e = None, None
    for e in range(e0, e1 + 1):
        if e in sums:
            last, last_e = sums[e][0] / sums[e][1], e
            angles.append(last); valid.append(True)
        elif last is not None and (e - last_e) * epoch_s <= max_gap_s:
            angles.append(last); valid.append(True)
        else:
            angles.append(last if last is not None else 0.0); valid.append(False)
    return e0 * epoch_s, angles, valid


def rolling_median_absdiff(angles: list[float], k: int) -> list[float]:
    """Centred window of k angles → median of the k-1 absolute successive differences; 0 at edges."""
    n = len(angles)
    out = [0.0] * n
    if n < k:
        return out
    d = [abs(angles[i + 1] - angles[i]) for i in range(n - 1)]
    m = k - 1                                         # diffs per window
    half = (k - 1) // 2                               # zoo centre alignment for even k: (k-1)//2 before
    win = sorted(d[:m])
    for i in range(n - k + 1):                        # window covers angles[i : i+k] → diffs d[i : i+m]
        mid = m // 2
        med = win[mid] if m % 2 else (win[mid - 1] + win[mid]) / 2
        out[i + half] = med
        if i + m < len(d):
            del win[bisect.bisect_left(win, d[i])]
            bisect.insort(win, d[i + m])
    return out


def quantile7(xs: list[float], p: float) -> float:
    """R's default quantile (type 7)."""
    s = sorted(xs)
    h = (len(s) - 1) * p
    lo = math.floor(h)
    return s[lo] + (h - lo) * (s[min(lo + 1, len(s) - 1)] - s[lo])


def _runs(flags: list[int]) -> list[list[int]]:
    """[[value, start, length], ...]"""
    runs = []
    for i, f in enumerate(flags):
        if runs and runs[-1][0] == f:
            runs[-1][2] += 1
        else:
            runs.append([f, i, 1])
    return runs


def detect(grav, epoch_s: int = EPOCH_S, max_gap_s: int = 600) -> Window | None:
    """HDCZA window over the whole input (pass roughly noon→noon, or one night with margin)."""
    t0, ang, valid = anglez_epochs(grav, epoch_s, max_gap_s)
    if len(ang) < (60 // epoch_s) * MIN_BLOCK_MIN:
        return None
    per_min = 60 // epoch_s
    x = rolling_median_absdiff(ang, ROLL_MIN * per_min)
    thr = min(TMAX, max(TMIN, quantile7(x, PCTL / 100) * MULT))
    nomov = [1 if (xi < thr and v) else 0 for xi, v in zip(x, valid)]
    runs = _runs(nomov)
    for r in runs:                                    # drop short still blocks
        if r[0] == 1 and r[2] <= per_min * MIN_BLOCK_MIN:
            r[0] = 0
    runs = _merge(runs)
    for j, r in enumerate(runs):                      # fill short interior movement gaps
        if r[0] == 0 and 0 < j < len(runs) - 1 and r[2] < per_min * MAX_GAP_MIN \
                and not any(not valid[i] for i in range(r[1], r[1] + r[2])):
            r[0] = 1
    runs = _merge(runs)
    still = [r for r in runs if r[0] == 1]
    if not still:
        return None
    best = max(still, key=lambda r: r[2])
    return Window(t0 + best[1] * epoch_s, t0 + (best[1] + best[2]) * epoch_s, thr, len(ang))


def _merge(runs):
    out = []
    for v, s, n in runs:
        if out and out[-1][0] == v:
            out[-1][2] += n
        else:
            out.append([v, s, n])
    return out


# ------------------------------------------------------------------ TIB + NOOP sleep → bands

def in_bed_bands(tib: Window | None, session: dict | None) -> dict:
    """Combine an HDCZA window with one NOOP session ({start,end,stages:[{start,end,stage}]}).

    TIB = union hull of the HDCZA window and the NOOP session (NOOP sleep always counts as in bed).
    Bands tile TIB: NOOP stages inside the session, and "inbed_wake" for TIB time before the first
    NOOP sleep epoch and after the last one (NOOP "wake" at the session edges folds into it).
    Returns {tib_start, tib_end, onset, final_wake, bands, sol_min, lie_in_min, waso_min, tst_min,
    tib_min, se}; times are unix s, None where undefined.
    """
    segs = sorted(session["stages"], key=lambda g: g["start"]) if session else []
    sleep = [g for g in segs if g["stage"] != "wake"]
    lo = [v for v in ((tib.start if tib else None), (session["start"] if session else None)) if v is not None]
    hi = [v for v in ((tib.end if tib else None), (session["end"] if session else None)) if v is not None]
    if not lo:
        return {"bands": []}
    t_start, t_end = min(lo), max(hi)
    onset = sleep[0]["start"] if sleep else None
    final = sleep[-1]["end"] if sleep else None
    bands = []
    if onset is None:
        bands.append({"start": t_start, "end": t_end, "stage": "inbed_wake"})
    else:
        if onset > t_start:
            bands.append({"start": t_start, "end": onset, "stage": "inbed_wake"})
        for g in segs:
            s, e = max(g["start"], onset), min(g["end"], final)
            if e > s:
                bands.append({"start": s, "end": e, "stage": g["stage"]})
        if t_end > final:
            bands.append({"start": final, "end": t_end, "stage": "inbed_wake"})
    tst = sum(b["end"] - b["start"] for b in bands if b["stage"] not in ("wake", "inbed_wake"))
    waso = sum(b["end"] - b["start"] for b in bands if b["stage"] == "wake")
    tib_s = t_end - t_start
    m = lambda s: round(s / 60, 1)
    return {"tib_start": t_start, "tib_end": t_end, "onset": onset, "final_wake": final, "bands": bands,
            "sol_min": m(onset - t_start) if onset is not None else None,
            "lie_in_min": m(t_end - final) if final is not None else None,
            "waso_min": m(waso), "tst_min": m(tst), "tib_min": m(tib_s),
            "se": tst / tib_s if tib_s else None}

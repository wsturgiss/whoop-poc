# HDCZA in-bed (TIB) layer

Experiment 2 from [open-sleep-data.md](open-sleep-data.md), run 2026-11-05. Goal: detect time in bed
from wrist z-angle so "awake in bed" = TIB minus NOOP sleep, before onset and after final wake.

## What was built

- [`hdcza.py`](../hdcza.py): stdlib port of HDCZA (van Hees et al. 2018, Sci Rep 8:12975,
  doi:10.1038/s41598-018-31266-z). It follows the GGIR `HASPT()` defaults (Apache-2.0; reimplemented, not
  vendored): z-angle per 5 s epoch → 5-min rolling median of |Δangle| → threshold = 15 × 10th percentile,
  clamped to [0.13°, 0.5°] → drop still blocks ≤ 30 min → fill movement gaps < 60 min → longest block.
  Gravity gaps > 10 min count as movement. `in_bed_bands()` merges the window with a NOOP session into
  bands (`inbed_wake` before first and after last NOOP sleep epoch, NOOP stages between) plus SOL,
  lie-in, WASO, TST, TIB and SE = TST/TIB.
- [`bench/tib_harness.py`](../bench/tib_harness.py): `walch` (HDCZA + NOOP combined, vs PSG) and `newcastle`
  (HDCZA edges only; no HR in that set; includes a stdlib GENEActiv .bin reader and a RemLogic export parser).
- [`bench/tib_night.py`](../bench/tib_night.py): one night from the local whoop.db (read-only), printing
  NOOP session, HDCZA window, TIB metrics and the bands.
- [`test_hdcza.py`](../test_hdcza.py): synthetic tests.
- Data, outside git: `~/data/sleep-accel/`, `~/data/newcastle-psg/` (Zenodo 1160410, CC BY 4.0, zip md5
  `8ebadbc55cb0e76230f293fc6d53f504`, as published).

Reproduce: `python3 -I bench/tib_harness.py walch [--v2]`, `python3 -I bench/tib_harness.py newcastle --wrist left|right`,
`python3 -I bench/tib_night.py --since "YYYY-MM-DD 12:00" --until "YYYY-MM-DD 12:00" --bed HH:MM --up HH:MM`.

## Wear side

Since 2026-11-05 the Whoop Strap 4 is worn on the **right** wrist (previously left). whoop.db nights
before that date are left-wrist. HDCZA z-angle thresholds and TIB edges may shift after the side change
until re-checked on right-wrist nights. HR-based NOOP staging should be unaffected.

## Results

Values are median (mean absolute error) in minutes; negative = earlier than the reference.

**Walch (31 lab nights, Apple Watch, HDCZA input = lights-off −12 h … +14 h)**

| | median | MAE |
|---|---|---|
| HDCZA start vs lights-off | +2.2 | 5.3 |
| HDCZA start vs PSG sleep onset | −7.2 | 10.4 |
| HDCZA end vs PSG final wake | +2.6 | 25.2 |
| PSG sleep-onset latency (SOL) | 10.5 | |
| SOL, NOOP alone (V1 / V2) | 0.0 / 1.4 | |
| SOL, HDCZA TIB + NOOP sleep (V1 / V2) | 0.0 / 2.1 | |
| Sleep efficiency PSG / NOOP V1 / TIB+V1 | 0.91 / 0.98 / 0.98 | |

Accel stops 1–25 min after PSG ends, so morning lie-in can't be tested on Walch.

**Newcastle (28 sleep-clinic nights, GENEActiv, both wrists, input = PSG start −2 h … PSG end)**

| | left | right |
|---|---|---|
| HDCZA start vs PSG start, median (MAE) | −2.2 (48.9) | +0.1 (54.4) |
| HDCZA start vs PSG sleep onset, median (MAE) | −24.1 (67.4) | −24.6 (65.2) |
| … within ±30 min | 13 / 28 | 12 / 27 |
| HDCZA end vs PSG final wake, median | +0.7 | +0.6 |
| PSG-wake share of epochs inside the window | 0.20 | 0.20 |
| Threshold (clamp hit) | 0.5° (max) | 0.5° (max) |

Caveats for Newcastle:
- The devices kept recording for hours after the study, probably off the wrist and lying still. Before
  the input was bounded to the PSG period, the end error was +275 min. WHOOP data needs a non-wear mask
  (NOOP's wrist-off signal) before HDCZA.
- PSG final wake falls within minutes of PSG end on most nights, so the end edge is mostly censored. On
  the 4 nights with ≥ 30 min of PSG after final wake, HDCZA ran on through that still, awake time (errors
  −158 … +298 min), which is what an in-bed marker should do.
- Four nights have no usable end because the 12 h recording stopped before final wake.
- The PSG "Recording Date" was a day off on 2 nights. They were shifted ±24 h, the same rule as van Hees'
  `psg-ncl-acc-spt-detection-eval`.

**Will's night 2026-11-04/05 (whoop.db, NOOP V1; self-report: bed ~23:48, woke ~06:15 still in bed)**

```
data          23:23 → 07:59
NOOP session  00:07 → 06:11
HDCZA window  00:06 → 06:11  (threshold 0.13°)
TIB start vs bed 23:48: +18.4 min · TIB end vs 06:15: −3.2 min · SOL 1.4 · lie-in 2.5 · SE 0.97
```

Per 2-min block, the median 5 s |Δz-angle| from 23:28 to 00:04 was 1.0–10° and HR was 65–89. From 00:06
it dropped to < 0.1° with HR around 61. From 06:12 to 06:48 it was 0.2–6.6°, against a threshold of 0.13°.

## Conclusions

1. **HDCZA marks getting into bed, not falling asleep.** It lands near lights-off or PSG start (Walch +2 min
   with 5 min MAE; Newcastle about 0 min median but with heavy tails) and 7–25 min before PSG sleep onset.
   So it's a usable TIB edge in the lab, where people lie still at lights-off.
2. **On its own it doesn't fix sleep-onset latency.** NOOP already calls sleep from the first still epoch, so
   TIB start ≈ NOOP onset and SOL stays about 0. Separating still-but-awake from asleep needs a cardiac cue
   (experiment 4: HR-level wake veto, SleepECG), not more motion features.
3. **It doesn't fix Will's 23:48 → 00:07 gap.** For the 18 minutes before 00:06 the wrist was actively moving
   (1–5° per 5 s, about 10–40× the threshold, HR up to 89). That looks like phone or reading in bed, and
   wrist angle can't tell it apart from sitting up on a sofa. HDCZA and NOOP agree on 00:06/00:07. The same goes for
   the morning: the wrist moves from 06:12, so neither method can know he stayed in bed.
4. **The gap looks fixable only with a signal that isn't wrist motion:**
   - a bed / up marker: NOOP `labels` table, or a one-key `whoop-poc bed` command
   - a desktop idle or lock time as a "not at the computer" hint
   - HR-based relaxation onset (falling HR/RR trend) to extend TIB back from onset across low-intensity
     movement, which experiment 4 could test

   With a bed marker, `in_bed_bands()` already produces the `inbed_wake` band and SOL/SE.

## Next

- Add a non-wear mask before HDCZA on whoop.db data, using NOOP's wrist-off detection or long exact-zero variance.
- Experiment 4 (HR wake veto) targets conclusion 2. Experiment 3 (logging bed/up times for 2–3 weeks)
  measures conclusion 4 directly, and `bench/tib_night.py --bed/--up` is the scorer for it.
- `in_bed_bands()` isn't wired into the whoop-poc HTML/TUI yet. Wire it in once a bed marker or HR cue
  actually moves the TIB edge.

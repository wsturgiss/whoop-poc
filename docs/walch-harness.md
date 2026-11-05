# Walch PSG harness: NOOP SleepStager vs polysomnography

Experiment 1 from [open-sleep-data.md](open-sleep-data.md), run 2026-11-05.

## Setup

- **Data:** PhysioNet [sleep-accel 1.0.0](https://physionet.org/content/sleep-accel/1.0.0/) (Walch et al. 2019,
  ODC-By 1.0). 31 subjects, one lab night each, with Apple Watch accel and PPG heart rate. PSG is scored in 30 s epochs.
  Downloaded to `~/data/sleep-accel/` (outside the repo). Zip md5 `b94e7fb6514ecb77942262f525b33fb7`,
  and all files passed `SHA256SUMS.txt`.
- **Harness:** [`bench/walch_harness.py`](../bench/walch_harness.py), stdlib only. It feeds each subject into the
  same `stager/.build/release/whoop-stage` binary whoop-poc uses, so it runs exactly NOOP's `SleepStager.detectSleep`
  (V1, or V2 staging with `--v2`).
  - gravity = 1 s mean of raw ~50 Hz accel (≈ what WHOOP's decode gives us)
  - HR = Apple Watch samples as-is (`--hr-fill N` forward-fills to 1 Hz; it made no difference)
  - **no RR and no resp** in this dataset, so V1's RMSSD/resp features and V2's RSA term receive nothing
  - PSG t=0 is anchored at a nominal **23:00 local**, because the stager's daytime guards read local clock time
  - Epochs outside any detected session count as wake. Mapping: N1+N2 → light, N3 → deep.
- **Reproduce:** `stager/build.sh && python3 -I bench/walch_harness.py [--v2] --json out/walch/v1.json`
  (about 2 s per subject. `out/` is gitignored.)

## Results (31 subjects, 26,773 scored epochs)

| Metric | V1 | V2 |
|---|---|---|
| Sleep/wake accuracy (pooled) | 0.86 | 0.87 |
| Sleep/wake κ (pooled) | 0.17 | 0.28 |
| Sleep sensitivity / **wake specificity** (mean) | 0.92 / **0.22** | 0.91 / **0.46** |
| 4-class accuracy (pooled; per-subject median) | 0.49; 0.52 | 0.54; 0.56 |
| 4-class κ (pooled; per-subject median) | 0.09; 0.07 | 0.30; 0.31 |
| PSG N3 epochs labelled deep | 9 % | 59 % |
| PSG REM epochs labelled REM | 20 % | 56 % |
| **Sleep-onset error** (pred − PSG), median (n=29) | **−9.0 min** | **−7.0 min** |
| …on nights with PSG sleep latency ≥ 15 min (n=8) | median −19.5 min | median −15.5 min |
| Session start vs lights-off, median | −0.5 min | (same: V1 window) |
| **Final-wake error, nights with ≥ 5 min real wake at the end** (n=5) | **median +17 min**, mean +18.6 | ≈ same |
| Sleep efficiency, PSG vs predicted (mean) | 0.91 vs 0.91 | 0.91 vs 0.88 |

Failures: one subject got no session at all, and two sessions started 3–6 h after lights-off. These count
against the scores above.

## What it tells us

1. **The stager has no "awake in bed" state.** The session starts at lights-off (median −0.5 min) and almost
   always counts as asleep from the first still epoch. Onset error is negative on all 29 nights, and on nights
   with long sleep latency it misses nearly all of it. At the other end, on the 5 nights where people lay awake after
   their last sleep, it kept scoring sleep for a median of 17 more minutes (up to 76 min). This is the same
   structural gap behind Will's 23:48 / 06:15 nights. In the lab people lay still at lights-off, so the session
   started *early*. At home Will moved first, so it started *late*. Either way, stillness is being used as a stand-in for sleep.
2. **Wake detection is the weakest part.** V1 catches 22 % of PSG-wake epochs. Accuracy of 0.86 looks good
   only because ~91 % of lab epochs are sleep. κ is the honest number here.
3. **V2's staging is a real improvement over V1 on PSG** (4-class κ 0.09 → 0.30, deep 9 % → 59 %, REM 20 % → 56 %).
   That's multi-subject support for its n=1 claim, even without its RSA input. V2 over-calls deep
   (3,277 light epochs → deep). It doesn't change session boundaries, because those come from V1.
4. **Caveats:** Apple Watch PPG HR is sparser and noisier than WHOOP's 1 Hz HR. No RR was available.
   Lab nights aren't home nights, and the subjects are healthy adults. The 23:00 anchor is a nominal time,
   not the subject's real clock time. Treat these as a lower bound for V1/V2 on WHOOP data, not as WHOOP numbers.

## Next

- Experiment 2 (HDCZA in-bed window) and experiment 4 (HR-level wake veto) both go after finding 1. This harness is
  now the regression test for them: the targets are an onset error near 0 on long-latency nights, an uncensored
  final-wake error near 0, and wake specificity well above 0.46.
- Re-run on DREAMT (it has IBI ≈ RR) to test the RMSSD/RSA paths this dataset can't exercise.

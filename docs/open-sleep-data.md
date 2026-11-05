# Open sleep data & research for HR + gravity sleep staging

Research note, 2026-11-05. Scope: what open data, models and papers could help whoop-poc improve
sleep onset, wake detection and "awake in bed" on a WHOOP 4.0 (1 Hz HR, RR intervals, gravity vector)
staged by NOOP's `SleepStager`. Everything below links to a primary source I checked. Anything I
couldn't confirm is marked **(unverified)**.

## Why the 23:48 bedtime became a 00:07 session start

NOOP's V1 `SleepStager` (`~/src/noop/Packages/StrandAnalytics/Sources/StrandAnalytics/SleepStager.swift`)
builds the session from a **gravity-stillness spine**. It uses a 15-min rolling window
(`stillWindowMin = 15`) with ≥70 % still samples (`stillFraction = 0.70`), then requires 3 consecutive sleep
epochs (`onsetPersistEpochs = 3`). HR only confirms those runs, and Cole–Kripke is computed alongside
as a cross-check. So **session start is the moment the wrist went still, not when you got into bed.**
Time spent awake in bed before that (reading, phone, shifting around) falls outside the session
entirely. V1 also deliberately suppresses still stretches after waking (H7 "morning residual
stillness"), so lying awake in bed after 06:15 is also left out. V2 (`SleepStagerV2.swift`, opt-in) only re-stages the window V1
already picked, so it has the same limit.

Commercial devices separate **time in bed (TIB)** from **sleep period / total sleep time (TST)**. The open literature
does too: actigraphy research treats the "sleep period time window" (SPT) as its own detection step,
and SOL (sleep onset latency) is TIB start → first sleep. We currently have one boundary where we need two.

## Top datasets

| Dataset | Signals | Labels | Size | License / access | Fit for us |
|---|---|---|---|---|---|
| **Walch / Apple Watch** — [PhysioNet sleep-accel 1.0.0](https://physionet.org/content/sleep-accel/1.0.0/) | Apple Watch raw 3-axis accel (g), PPG-derived HR (bpm), steps | PSG stages per 30 s epoch (0 W, 1 N1, 2 N2, 3 N3, 5 REM) | 31 subjects, 1 night each, 550 MB zip | **ODC-By 1.0**, open download, no login | ★★★ Closest match to us: wrist HR + gravity, open, small. No RR intervals, so RMSSD/RSA features can't be tested on it. Recording starts at PSG lights-off, so there's little pre-bed data. |
| **Newcastle PSG + GENEActiv** — [Zenodo 1160410](https://zenodo.org/records/1160410) (van Hees et al. 2015, PLoS ONE) | Raw GENEActiv accel on **both wrists** (.bin) | PSG sleep scores, 30 s | 28 sleep-clinic patients, 963 MB | **CC BY 4.0**, open | ★★★ Best open set for **onset/wake boundary** work from gravity/angle alone. It's the dataset HDCZA was validated on. Clinic patients, so they sleep worse than average. |
| **DREAMT v2.2.0** — [PhysioNet](https://physionet.org/content/dreamt/) (Wang et al., CHIL 2024, PMLR 248) | Empatica E4: BVP 64 Hz, ACC 32 Hz, IBI, HR 1 Hz, EDA, TEMP. Full PSG in the 100 Hz folder | Technician stages W/N1/N2/N3/R per 30 s. The PSG "preparation" period is labeled W | 100 participants | **PhysioNet Restricted Health Data License 1.5.0**: needs a PhysioNet account and a signed DUA (no CITI course stated) | ★★★ Has **IBI + 1 Hz HR + accel**, which is closest to WHOOP's RR + HR + gravity. Cohort was referred for suspected apnea. v2.2.0 (June 2026) fixed an ACC↔PSG misalignment, so use ≥2.2.0. |
| **MESA Sleep** — [NSRR](https://sleepdata.org/datasets/mesa) | Full PSG including finger PPG. Actiwatch Spectrum for ~1 week, but **only 30 s epoch summaries** (no raw accel) | PSG stages, plus actigraphy epoch/interval data | 2,237 with actigraphy, ~2,000 with PSG | Free, but needs an **NSRR data request** (Data Access Committee review, roughly 1–2 weeks) and a DUA | ★★ Large-scale training and benchmarking. Standard source for PPG/HR staging (SleepPPG-Net, Zhai et al.). Actiwatch counts ≠ our gravity signal. NSRR notes PSG and actigraphy were scored independently. |
| **CMI Detect Sleep States** — [Kaggle](https://www.kaggle.com/competitions/child-mind-institute-detect-sleep-states) | Wrist accel reduced to `anglez` + `enmo` every 5 s, multi-day free-living | **Sleep onset / wakeup events** (annotated, not PSG) | ~300 children/teens, 500+ multi-day series | **CC BY-NC-SA 4.0** according to a [mirror's docs](https://ambiqai.github.io/sleepkit/datasets/cmidss); check the official Kaggle rules tab **(unverified)** | ★★ The only large open set whose label is exactly **"when did sleep start/end"** in free-living conditions. Many open winning solutions exist. It has pediatric bias and no HR. |
| **CAPTURE-24** — [Chan et al., Sci Data 2024](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11484779/) / [repo](https://github.com/OxWearables/capture24) | Axivity AX3 wrist accel, free-living | Camera + **sleep diary** annotations | 151 adults, ~3,900 h | Paper is CC BY 4.0. Data/code license not asserted on GitHub **(unverified)** | ★ Diary-level sleep labels, no HR. Useful only as a free-living sanity check for wake vs. still-and-awake. |

Not useful for us: Sleep-EDF, SHHS, CAP. They have EEG/ECG but no wrist motion. (SHHS ECG *is*
useful if we train an HR-only model, which is how SleepECG and SleepPPG-Net pretrain.)

## Open models / code

| Project | What it is | License | Notes |
|---|---|---|---|
| [ojwalch/sleep_classifiers](https://github.com/ojwalch/sleep_classifiers) | Reference code for Walch 2019 (HR + motion + circadian proxy, logistic regression/RF/NN) | **No license file** (all rights reserved by default) | Read it to understand the method and reimplement. Don't vendor it. NOOP V1 already ports its DoG-HR feature. |
| [cbrnr/sleepecg](https://github.com/cbrnr/sleepecg) (JOSS 2023, [paper](https://joss.theoj.org/papers/10.21105/joss.05411)) | HR-only sleep staging from **heartbeat times**. Ships pretrained GRU classifiers trained on NSRR records | **BSD-3-Clause** | ★ Can run on WHOOP RR-derived beat times locally, which gives a second opinion from a model trained on PSG data. `pip install sleepecg`. |
| [wadpac/GGIR](https://github.com/wadpac/GGIR) — **HDCZA** ([van Hees et al. 2018, Sci Rep 8:12975](https://doi.org/10.1038/s41598-018-31266-z)) | Detects the sleep period window from wrist z-angle variance, no diary needed. Validated on the Newcastle PSG set and on diaries (n≈3,752) | **Apache-2.0** (R) | ★ Directly answers "when did the sleep period start/end" using **only gravity angle**, which we already decode. Simple enough to port to Python. |
| [OxWearables/asleep](https://github.com/OxWearables/asleep) ("Self-supervised learning of accelerometer data provides new insights for sleep and its association with mortality") | Wrist-accel sleep/wake + REM/NREM. Trained on 1,000+ PSG nights. Reports TST and efficiency | **Oxford Academic Use Licence**: non-commercial academic research only; weights download on first run | Usable as a personal benchmark if you're comfortable with "academic research" terms. Accel-only. Needs CSV `time,x,y,z` at raw rate (we have gravity, not raw accel, so expect a domain gap). |
| [bzhai/multimodal_sleep_stage_benchmark](https://github.com/bzhai/multimodal_sleep_stage_benchmark) — Zhai et al. 2020, IMWUT 4(2):67 "Making Sense of Sleep" | MESA benchmark: actigraphy + HR features, ML/DL, 2–5 stages | **MIT** | Good recipe and feature list for training on MESA. Finding: movement beats cardiac-only for sleep/wake, while cardiac helps the stage classes. |
| SleepPPG-Net — [Kotzen et al., arXiv 2202.05735](https://arxiv.org/abs/2202.05735) (IEEE JBHI 2023) | Raw-PPG 4-class staging. Trained on MESA, pretrained on SHHS ECG. Median κ 0.75 on held-out MESA | Paper only; no official weights found | Shows the ceiling with **raw PPG**. We only get HR/RR from the strap, so this is a reference point and not something we can deploy. |

## Papers worth reading (sleep onset, wake, efficiency)

- **Walch et al. 2019, *Sleep*** (dataset above). This is the source of NOOP's quoted EEG-free ~65–73 % 4-class
  ceiling. Adding a circadian proxy feature helped.
- **van Hees et al. 2015, PLoS ONE** and **2018, Sci Rep** (HDCZA). Sleep-window detection from wrist angle,
  with no significant onset/wake bias vs PSG in their samples. This is the canonical open "SPT window" method.
- **te Lindert & Van Someren 2013**: the 30 s Cole–Kripke weights NOOP V1 already uses.
- Consumer-device validation papers consistently find **WASO and sleep onset are the weakest metrics**
  (wake gets underestimated). One example is the six-device lab comparison incl. WHOOP
  ([UHasselt PDF](https://documentserver.uhasselt.be/bitstream/1942/46017/1/A%20performance%20validation%20of%20six%20commercial.pdf)).
  An HR + accel model will see still-but-awake epochs as sleep unless it has a separate TIB/posture cue
  or an HR-level cue. That is exactly our 23:48 → 00:07 gap.

## How this maps to NOOP's assumptions

- NOOP **V1** = stillness spine + Cole–Kripke cross-check + Walch DoG-HR + RMSSD/resp percentile bands +
  physiology rules. Every one of those pieces can be checked against the open data above.
- NOOP **V2** = per-night z-scores, a deep gate based on HR flatness, a jerk wake gate, RSA, and HMM smoothing, with
  coefficients "fixed a-priori, not fit". Its own header says it was validated on **n=1, 7 nights,
  against a commercial reference** (κ ~0.06 → ~0.47). So neither V1 nor V2 has been tested against PSG on
  multiple subjects. Walch + DREAMT would be the cheapest way to run that test.
- Licensing: NOOP is **PolyForm Noncommercial 1.0.0**. Everything above (ODC-By, CC BY, BSD, MIT, Apache,
  NSRR DUA, PhysioNet DUA, CC BY-NC-SA, Oxford academic) works for a **personal, non-commercial Linux PoC**.
  Don't commit datasets to this repo; DUAs (DREAMT, MESA) forbid redistribution. Keep them under e.g.
  `~/data/sleep/` and point to them with an env var.

## Practicality (local training/validation)

All of these fit on a laptop. Walch is 2.2 GB unzipped, Newcastle ~1 GB, and DREAMT and MESA are larger,
but you only need the wrist channels plus hypnograms. The models involved (logistic regression/RF/GRU/HMM) train on CPU in
minutes. The main real work is **domain adaptation**: WHOOP gives 1 Hz HR + RR + a decoded *gravity* vector,
while these datasets give raw accel (Walch, Newcastle, DREAMT), counts (MESA) or BVP/IBI (DREAMT). Low-pass
filtering raw accel to gravity and resampling HR to 1 Hz gets reasonably close.

## Next experiments for whoop-poc

1. **Build a PSG harness on Walch (open, today).** Write a script that turns `sleep-accel` into the inputs
   `stager/whoop-stage` expects: HR at 1 Hz, gravity = low-passed accel, no RR. Run NOOP V1/V2 and report
   epoch accuracy, Cohen's κ (2-class and 4-class), and **onset/wake error in minutes**. That gives us
   our first real numbers instead of the borrowed "65–73 %". Add DREAMT (IBI ≈ RR) after signing the DUA
   so the RMSSD/RSA paths get tested too.
2. **Add a separate in-bed (TIB) layer using HDCZA.** Port HDCZA (z-angle variance, Apache-2.0 reference)
   to Python on our gravity stream. Validate its window edges on the Newcastle Zenodo PSG set. Then render
   `in-bed, awake` = TIB window minus NOOP sleep, shown as its own hypnogram band before onset and after
   final wake, and compute SOL, efficiency = TST/TIB and morning lie-in. This targets this morning's
   23:48 vs 00:07 gap directly.
3. **Collect personal ground truth (n=1, cheap, highest value).** Log lights-out and out-of-bed times every night
   for 2–3 weeks. NOOP's `labels` table or a small CSV would work. Score V1, V2 and HDCZA on |onset error|,
   |wake error| and TIB error. Doubles as a regression test.
4. **Get a second opinion from an HR-only model.** Feed WHOOP RR-derived beat times into SleepECG's pretrained
   classifier (BSD-3, trained on PSG data) and compare its wake epochs to NOOP's. In particular, check whether
   HR stays elevated during the pre-onset stillness, which would give us an **HR-level wake veto**: still wrist
   but HR above the night's sleep band means awake in bed.
5. **(Later) Train a small onset/wake event detector.** Use CMI Kaggle `anglez`/`enmo` (non-commercial) and
   MESA (NSRR request) to train a lightweight onset/wakeup event model, then fine-tune or check it on Walch +
   personal labels. Only worth doing if experiments 2–4 still leave >10 min of systematic onset error.

## Sources

- Walch et al. dataset: https://physionet.org/content/sleep-accel/1.0.0/ · code: https://github.com/ojwalch/sleep_classifiers
- DREAMT: https://physionet.org/content/dreamt/ (doi:10.13026/3f7y-2d80)
- Newcastle PSG + accel: https://zenodo.org/records/1160410
- MESA / NSRR: https://sleepdata.org/datasets/mesa · actigraphy intro: https://sleepdata.org/datasets/mesa/pages/actigraphy-introduction.md
- CMI Detect Sleep States: https://www.kaggle.com/competitions/child-mind-institute-detect-sleep-states
- CAPTURE-24: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11484779/
- HDCZA: https://doi.org/10.1038/s41598-018-31266-z · GGIR: https://github.com/wadpac/GGIR
- SleepECG: https://github.com/cbrnr/sleepecg · https://joss.theoj.org/papers/10.21105/joss.05411
- asleep: https://github.com/OxWearables/asleep
- Zhai et al. benchmark: https://github.com/bzhai/multimodal_sleep_stage_benchmark
- SleepPPG-Net: https://arxiv.org/abs/2202.05735

## Results log

- **2026-11-05, Experiment 1 done:** [walch-harness.md](walch-harness.md). On 31 Walch PSG nights: V1 wake
  specificity 0.22, 4-class κ 0.09. V2 4-class κ 0.30. Sleep onset is called early on every night (median −9 min,
  −19.5 min when sleep latency ≥ 15 min), and lying awake after the final wake is scored as sleep (median +17 min,
  n=5). This confirms we need an in-bed layer separate from sleep (experiments 2 and 4).

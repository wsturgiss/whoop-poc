# whoop-poc

A thin proof of concept that shows **heart rate** and **sleep** from a WHOOP 4.0 strap on Linux. It uses no phone and no WHOOP cloud.

It reads the local SQLite store written by [NOOP](https://github.com/ParthJadhav/noop)'s
`tools/linux-capture/whoop_sync.py`. It doesn't do any Bluetooth itself: syncing is delegated to that tool.

- **TUI** ([Textual](https://textual.textualize.io/)) shows latest, resting, range and worn-time HR, a per-minute HR sparkline, a sleep table with stage totals and a colored stage bar. It reloads automatically, and `s` syncs the strap.
- **HTML** is a single static page with an HR chart, shaded sleep windows, a stage hypnogram per session and a sleep table, with light and dark themes.
- **summary** is a plain-text printout.

## Run

```bash
uv run whoop-poc              # TUI   (q quit · r reload · s sync strap · d dark/light)
uv run whoop-poc html         # writes out/whoop.html → open it in a browser
uv run whoop-poc html --watch # regenerate every 30s (page auto-refreshes)
uv run whoop-poc summary      # plain text
uv run whoop-poc --demo       # synthetic night pinned to 2020-01-01, to preview the sleep view
```

Options: `--db PATH` (default `~/src/noop/tools/linux-capture/captures/whoop.db`, or env `WHOOP_DB`),
`--hours 24`, `--sync` (drain the strap first), `--address MAC`.

The DB is opened **read-only**. Syncing runs `whoop-sync sync` if that wrapper is on `PATH`. Otherwise it runs
linux-capture's `whoop_sync.py` directly with `WHOOP_ADDRESS` (and optionally `WHOOP_CAPTURE_DIR`).

## What's decoded

These are WHOOP 4.0 type-47 `HISTORICAL_DATA` records, firmware layout v12 (the same as v24 per NOOP's `whoop_protocol.json`). Each record is one per second and gives:
unix time `@11`, heart rate `@21`, and the gravity vector as f32 x/y/z `@40/44/48`. A record is rejected if |g| falls outside 0.5–1.5 g.

## Time zones

The HTML page renders every clock time in the **viewing browser's time zone** and labels the axis with the zone name (for example `America/New_York (EST)`). Without JavaScript it falls back to the generating machine's local zone. The TUI uses the terminal's local zone and shows its name in the header.

## Sleep stages (NOOP SleepStager)

If `stager/.build/release/whoop-stage` exists, sleep comes from **NOOP's own `SleepStager`**
(`Packages/StrandAnalytics`) run on NOOP's decoded `feat_second` and `feat_rr` tables. That gives sessions with
**wake / REM / light / deep** stages, resting HR and HRV. NOOP's own caveat applies: the stages are approximate, not
validated against a lab sleep study, with roughly 65–73% agreement per 30-second chunk at best, and deep sleep is the least reliable stage.
Without the binary, or with `--heuristic`, it falls back to the simple heuristic below.

Build it on Linux:

```bash
git -C ~/src/noop sparse-checkout add Packages                         # NOOP's Swift packages
swiftly init --no-modify-profile --platform ubuntu24.04 -y             # Swift toolchain (user-local)
stager/build.sh --test                                                 # links NOOP sources, builds, runs NOOP's 80 stager tests
```

- `stager/` compiles NOOP's StrandAnalytics **minus 7 files** (AnalyticsEngine, ReadinessEngine and the
  score/trace engines that depend on them). Those need `WhoopStore`, which imports Apple's `Compression` framework. That framework doesn't exist on Linux,
  and the sleep stager doesn't use it. NOOP's sources are symlinked in at build time and never committed here.
- The decoded tables come from NOOP's `whoop-decode`
  (`swift build -c release --product whoop-decode` in `Packages/WhoopProtocol`, then `whoop_sync.py decode`).
- **Arch/Omarchy:** swiftly ships the Ubuntu toolchain, which wants `libncurses.so.6` and `libxml2.so.2`. Symlink
  `/usr/lib/libncursesw.so.6` and `/usr/lib/libxml2.so.16` under those names into
  `~/.local/share/swiftly/toolchains/<ver>/usr/lib/swift/linux/`. SwiftPM strips `LD_LIBRARY_PATH`, so they
  must sit on the toolchain's RUNPATH.

## Sleep is an approximation (fallback heuristic)

This is **not** WHOOP's sleep staging. It's a simple actigraphy-style heuristic:

1. Bucket each minute and take its mean per-second change in gravity (wrist motion) and its mean HR.
2. A minute is *still + calm* when motion < 0.05 g, the strap is worn, and HR is at least 5 bpm below the median HR of the minutes when the wrist was moving.
3. Merge still + calm minutes across gaps of 20 minutes or less. A window counts as sleep only if it lasts 60 minutes or more and at least 60% of its minutes are still + calm. Sitting still in the evening is not sleep.
4. Minutes covered by an `awake` label in NOOP's labels table are never counted as sleep. Add one with
   `whoop_sync.py label --activity awake --start 2026-11-04T23:00 --end 2026-11-04T23:48 ...`

"Asleep ≈" is the count of still + calm minutes inside the window. Expect errors: lying still while awake
counts as sleep, and restless sleep counts as awake. Real staging (light/deep/REM) would need NOOP's Swift
`whoop-decode` + StrandAnalytics pipeline, which is out of scope for this PoC.

## Data safety

Nothing biometric is committed: `*.db`, exports and generated HTML are gitignored. The tests use synthetic frames only.

```bash
uv run python -m unittest -v
```

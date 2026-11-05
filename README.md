# whoop-poc

A thin proof of concept that shows **heart rate** and **sleep** from a WHOOP 4.0 strap on Linux. It uses no phone and no WHOOP cloud.

It reads the local SQLite store written by [NOOP](https://github.com/ParthJadhav/noop)'s
`tools/linux-capture/whoop_sync.py`. It doesn't do any Bluetooth itself: syncing is delegated to that tool.

- **TUI** ([Textual](https://textual.textualize.io/)) shows latest, resting, range and worn-time HR, a per-minute HR sparkline, a table of sleep windows and a still/restless bar. It reloads automatically, and `s` syncs the strap.
- **HTML** is a single static page with an HR chart, shaded sleep windows and a sleep table, with light and dark themes.
- **summary** is a plain-text printout.

## Run

```bash
uv run whoop-poc              # TUI   (q quit · r reload · s sync strap · d dark/light)
uv run whoop-poc html         # writes out/whoop.html → open it in a browser
uv run whoop-poc html --watch # regenerate every 30s (page auto-refreshes)
uv run whoop-poc summary      # plain text
uv run whoop-poc --demo       # synthetic night, to see the sleep view before you have real sleep data
```

Options: `--db PATH` (default `~/src/noop/tools/linux-capture/captures/whoop.db`, or env `WHOOP_DB`),
`--hours 24`, `--sync` (drain the strap first), `--address MAC`.

The DB is opened **read-only**. Syncing runs `whoop-sync sync` if that wrapper is on `PATH`. Otherwise it runs
linux-capture's `whoop_sync.py` directly with `WHOOP_ADDRESS` (and optionally `WHOOP_CAPTURE_DIR`).

## What's decoded

These are WHOOP 4.0 type-47 `HISTORICAL_DATA` records, firmware layout v12 (the same as v24 per NOOP's `whoop_protocol.json`). Each record is one per second and gives:
unix time `@11`, heart rate `@21`, and the gravity vector as f32 x/y/z `@40/44/48`. A record is rejected if |g| falls outside 0.5–1.5 g.

## Sleep is an approximation

This is **not** WHOOP's sleep staging. It's a simple actigraphy-style heuristic:

1. Bucket each minute and take its mean per-second change in gravity (wrist motion) and its mean HR.
2. A minute is *still + calm* when motion < 0.05 g, the strap is worn, and HR is below the median HR of the minutes when the wrist was moving.
3. Merge still + calm minutes across gaps of 20 minutes or less, and keep runs of 30 minutes or more as sleep windows.

"Asleep ≈" is the count of still + calm minutes inside the window. Expect errors: lying still while awake
counts as sleep, and restless sleep counts as awake. Real staging (light/deep/REM) would need NOOP's Swift
`whoop-decode` + StrandAnalytics pipeline, which is out of scope for this PoC.

## Data safety

Nothing biometric is committed: `*.db`, exports and generated HTML are gitignored. The tests use synthetic frames only.

```bash
uv run python -m unittest -v
```

# whoop-poc

A thin proof of concept that shows **heart rate** and **sleep** from a WHOOP 4.0 strap on Linux. It uses no phone and no WHOOP cloud.

It reads the local SQLite store written by [NOOP](https://github.com/ParthJadhav/noop)'s
`tools/linux-capture/whoop_sync.py`. It doesn't do any Bluetooth itself: syncing is delegated to that tool.

- **TUI** ([Textual](https://textual.textualize.io/)) shows latest, resting, range and worn-time HR, a per-minute HR sparkline, a sleep table with stage totals and a colored stage bar. It reloads automatically. `s` starts a background sync (the systemd unit if installed), and an animated banner shows its progress; `q` quits right away, even mid-drain, because drains resume safely.
- **HTML** is a single static page with an HR chart, shaded sleep windows, a stage hypnogram per session and a sleep table, with light and dark themes.
- **summary** is a plain-text printout.
- **json** / **serve** emit a versioned JSON snapshot (latest HR, per-minute HR, sleep sessions + stages,
  sync freshness) for other programs. The contract is in [COACH.md](COACH.md).

## Install (per user, no root)

```bash
contrib/install.sh --address AA:BB:CC:DD:EE:FF   # whoop-poc + whoop-sync on PATH, sync timer enabled
```

See [PLUGIN.md](PLUGIN.md) for what goes where and the plan for an Omarchy bar widget.

## Background sync

`whoop-sync.timer` runs `whoop-sync sync` 15 min after the previous attempt finished. Each attempt
drains for up to `WHOOP_SYNC_MAX` seconds (default 1200), which is below the unit's
`TimeoutStartSec=30min` so systemd never kills a drain mid-flight. Then `coach.json` is refreshed.

**Resume:** NOOP commits each chunk (fsync) *before* acking it, and the strap only trims acked chunks. So a drain
that stops early (time cap, SIGTERM, BLE drop, strap out of range) loses nothing. The next run continues
from the strap's cursor. A long backlog (such as a whole night) can take two or three runs. Check progress with:

```bash
cat ~/.local/state/whoop-poc/sync.json      # last attempt: complete | partial | not_advertising | error
journalctl --user -u whoop-sync -n 40
whoop-sync status
```

**Battery:** each sync first reads the strap battery with WHOOP's own `GET_BATTERY_LEVEL` command
(`whoop-sync battery` does it by hand) into `~/.local/state/whoop-poc/battery.json`. The standard BLE
Battery Service reports a flat 100% on this strap and is ignored.

**Live HR** (`l` in the TUI, or `whoop-sync live --minutes N`): arms only WHOOP's realtime-HR toggle and
reads bpm from the strap's REALTIME_DATA frames. The standard BLE Heart Rate characteristic is silent on
this strap because its HR-broadcast profile is off. NOOP's full realtime stream (R-R + raw IMU) is used only
as a fallback. Holding the link open costs strap battery, so it always
auto-stops: 30 min by default (`WHOOP_LIVE_MINUTES`), `+` in the TUI adds 10 min (3 h cap), and the TUI
stops it when you quit. While it runs it holds the strap, so
timer syncs skip. Pressing `l` during a drain pauses the drain, which resumes on the next timer run.

**Stopping a drain** (`systemctl --user stop whoop-sync`, Ctrl+C) is safe: the drain saves, decodes and is
recorded as `partial`, and the next run resumes.

`not_advertising` means the strap wasn't connectable. The usual causes are the phone's WHOOP app holding it (turn phone
Bluetooth off), being out of range, or the strap idling. The timer just retries.

## Run

```bash
uv run whoop-poc              # TUI   (q quit · r reload · s sync · l live HR · + extend live · w window · d dark/light)
uv run whoop-poc html         # writes out/whoop.html → open it in a browser
uv run whoop-poc html --watch # regenerate every 30s (page auto-refreshes)
uv run whoop-poc summary      # plain text
uv run whoop-poc --demo       # synthetic night pinned to 2020-01-01, to preview the sleep view
uv run whoop-poc json         # snapshot JSON to stdout (--out FILE writes atomically, mode 600)
uv run whoop-poc serve        # GET http://127.0.0.1:8787/v1/snapshot?hours=24
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

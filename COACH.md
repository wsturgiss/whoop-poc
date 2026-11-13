# Reading WHOOP data from another program (Coach)

Coach is an LLM assistant, so what it reads by default is a **summary**: one row per day, detected
workouts, data gaps, freshness and trends: about 2–3.5 KB for a week (see [Size](#size)). Per-minute HR and sleep
stage segments stay in `whoop.db` and the daily archive, and Coach asks for them one small piece at a
time. Never scrape the TUI or HTML.

| What | How | Size |
|---|---|---|
| **Summary** (`whoop-poc/snapshot@3`) | file `~/.local/state/whoop-poc/coach.json`, or `whoop-poc coach`, or `GET http://127.0.0.1:8787/v1/coach` | ~2–3.5 KB |
| New since a cursor | `whoop-poc coach since <cursor_unix>`, or `GET /v1/coach?since=<unix>` | smaller |
| One day | `whoop-poc coach day 2026-11-11` (also `today`, `yesterday`) | ~1–2 KB |
| One workout, computed detail (`whoop-poc/workout@2`) | `whoop-poc coach workout <id>`, e.g. `2026-11-11-0824` (also `start_unix`, `HH:MM`, `2026-11-11T08:24`) | ~1 KB |
| Record Will's duration | `whoop-poc coach confirm <id> --minutes N [--start HH:MM]`; `--clear` removes it | < 1 KB |
| Daily archive (`whoop-poc/day@2`) | `~/.local/state/whoop-poc/daily/YYYY-MM-DD.json` | ~15–25 KB per day |
| Per-minute snapshot (`whoop-poc/snapshot@1`) | `whoop-poc json --raw [--hours 24]`, `GET /v1/snapshot?hours=24` | ~45 KB per day |

`whoop-sync.service` refreshes `coach.json` and the archive after every sync attempt (`ExecStopPost`
runs `whoop-poc json --out coach.json`), roughly every 15–35 min. Files are written atomically
(tmp + rename), mode 600, in a 700 directory. It is biometric data: it lives outside the repo and is
never committed. All CLI drill-downs print one line of compact JSON to stdout.

## Recommended read flow

1. **Read the summary.** Read `coach.json` (or run `whoop-poc coach`). Check `freshness` before
   believing anything: a large `data_age_min` means the strap hasn't synced (see `last_sync.outcome`).
   If `freshness.hint` is present, pass it on to Will as it is rather than guessing a cause.
2. **Remember `cursor_unix`.** It is the newest sample's time. Store it as `last_seen`.
3. **Next time, fetch only what's new.** Run `whoop-poc coach since <last_seen>`. It returns the same
   document, with `days`, `workouts` and `gaps` limited to records that end after `last_seen` (plus
   workouts confirmed after it). An open
   day (today) always comes back, because its numbers grow. Returned records replace what you have:
   a day row with the same `date`, or a workout with the same `start_unix`, supersedes the old one.
4. **If `last_seen` is older than the summary window** (`window.since_unix`), `since` already reaches
   back up to 30 days from the DB. For anything older, or if Coach can only read files, read
   `daily/YYYY-MM-DD.json` for each missing date. Those files keep per-minute HR, so read them only for
   the days you need.
5. **Confirm new workouts.** For each workout with `needs_confirm: true`, ask Will how long it
   really was (and when it started, if the detected start looks off), then record his answer:
   `whoop-poc coach confirm <start_unix> --minutes N [--start HH:MM]`. Ask once per workout; a
   confirmed one has `needs_confirm: false`. See [Confirmed durations](#confirmed-durations).
6. **Drill down only when a question needs it**: `coach day <date>` for one day's hourly HR and all its
   sleep sessions, `coach workout <id>` for a workout's computed detail (peaks, recovery, drift,
   intervals; see [Workout detail](#workout-detail)).

Using data time (`cursor_unix`) rather than read time as the cursor matters: the strap is drained in
batches, so a workout at 08:24 can land in the DB at 09:40. The strap drains oldest-first, so nothing
newer than the cursor is ever skipped.

## Contract (all schemas)

- `schema` names the document. Within a version, fields are only ever **added**. A rename, removal or
  meaning change bumps the version. Readers should check the prefix and ignore unknown keys.
- Every record carries unix seconds (`*_unix`). `local` strings are the same times in `tz`
  (`America/New_York` unless `WHOOP_TZ` is set), for reading. Days are local calendar days in `tz`.
- A missing key or `null` means "not known" or "none", never zero.
- `demo: true` marks synthetic data (`--demo`). Never treat it as the wearer's. Demo is never archived.
- HR values in rows and workouts are 1-minute means (`max_hr` = the highest 1-minute mean).
  Zones are by heart-rate reserve: zone low bound = `rest_hr + fraction × (max_hr − rest_hr)`, with
  Z1–Z5 at 50/60/70/80/90%. `zones` in the summary gives the bpm bounds in use, plus `max_hr_source`
  and `observed_peak` (see "Max HR" below).

## Max HR

`max_hr` is resolved in this order; the first that applies wins:

1. **Override**: `--max-hr N` on any command, else `WHOOP_MAX_HR` (environment, then
   `~/.config/whoop-poc/env`). `max_hr_source: "override"`.
2. **Observed**: the clean observed 5-s peak, when it is above 190. `max_hr_source: "observed"`.
3. **Default**: 190. `max_hr_source: "default"`.

The observed peak only ever raises max_hr above the default; a submaximal effort never lowers the zones.
It is the highest mean over 5 consecutive seconds (≥ 4 readings, as `peak_hr_5s`) after dropping readings
outside 30–230 bpm and one-off spikes (> 10 bpm from every reading within 2 s), and only where that mean is
within 15 bpm of the mean over the 30 s either side, so a short burst of optical noise is not a peak.
Every `coach` run and TUI reload checks the HR it loaded and raises the stored peak
(`~/.local/state/whoop-poc/max-hr.json`, never whoop.db) when it finds a higher one.
`whoop-poc coach maxhr` scans all stored HR and prints `zones`; `--clear` forgets the stored peak first.

`observed_peak` is `{peak_hr_5s, peak_unix, local, workout_id}` (`workout_id` null when the peak is outside
a detected workout), or null before any scan. Zone minutes are always computed with the max_hr in use, so
`zone_min` in the summary, day views and workout views follows a change. Daily files carry the `zones` they
were written with; a final day still inside the summary window is rewritten when the bounds change, older
files keep theirs.

## Summary: `whoop-poc/snapshot@3`

```jsonc
{
  "schema": "whoop-poc/snapshot@3", "demo": false,
  "generated_unix": 1794408430, "tz": "America/New_York",
  "window": {"since_unix": …,          // local midnight, `days` calendar days ago incl. today
             "days": 7,                  // WHOOP_COACH_DAYS
             "data_since_unix": …},      // first worn minute in the window (earlier = no data at all)
  "cursor_unix": 1794408217,             // newest sample; next time call `coach since <this>`
  "freshness": {"data_age_min": 3,       // now − newest sample
                "last_sync": {"outcome": "complete|partial|not_advertising|error", "finished_unix": …},
                "battery_pct": 44,
                "hint": "strap out of range of baconstation (not advertising); no new data for 3 h 7 min"},
                                         // hint: only when last_sync is not_advertising and data is > 60 min old
  "latest_hr": {"unix": …, "bpm": 96},
  "zones": {"max_hr": 190, "rest_hr": 60, "Z1": 125, "Z2": 138, "Z3": 151, "Z4": 164, "Z5": 177,
            "max_hr_source": "default",
            "observed_peak": {"peak_hr_5s": 182, "peak_unix": …, "local": "11-11 08:42:08", "workout_id": "2026-11-11-0824"}},
  "sleep_source": "noop|heuristic",
  "days": [{                             // one per local day, oldest first
    "date": "2026-11-11", "start_unix": …,
    "open": true,                        // present while the day can still change (not over, or not drained past its end)
    "no_data": true,                     // present (alone) when the day has no worn minute at all; see below
    "worn_min": 584,                     // minutes with HR; absent on no_data days
    "unworn_min": 19,                    // minutes without HR between the first and newest data (strap off)
    "rest_hr": 62,                       // ~5th pct of still minutes; only with ≥ 120 worn min
    "avg_hr": 76, "max_hr": 172,
    "zone_min": {"Z1": 5, "Z3": 14},     // minutes per zone, zeros left out
    "active_min": 24,                    // workout duration_min + Z1+ minutes outside workouts (see Trends)
    "sleep": {                           // the longest night ending this day (last night belongs to today); naps excluded
      "start_unix": …, "end_unix": …, "local": "23:59-08:16",   // in bed = start..end
      "asleep_min": 404, "eff_pct": 81,
      "resting_hr": 49,                  // NOOP stager; the heuristic gives "avg_hr" (mean asleep HR) instead
      "stage_min": {"wake": 11, "rem": 40, "light": 150, "deep": 47}},   // NOOP stager only
    "sleep_sessions": 2,                 // present when more than one night session ended this day
    "nap_min": 40                        // asleep minutes in naps ending this day (see Sleep below)
  }],
  "workouts": {"cols": ["start_unix", "end_unix", "local", "duration_min", "avg_hr", "max_hr", "zone_min",
                        "detected_min", "needs_confirm", "id"],
               "rows": [[1794403440, 1794404760, "11-11 08:24-08:46", 20, 157, 172, {"Z1": 2, "Z2": 2, "Z3": 14, "Z4": 4},
                         22, false, "2026-11-11-0824"]]},   // duration_min = Will's 20; detected_min = the HR window, 22
  "gaps": [{"start_unix": …, "end_unix": …, "local": "13:05-15:40", "min": 155}],   // ≥ 30 min without worn data
  "trends": {                            // latest value vs the mean of the earlier days that have it
    "rest_hr":          {"latest": 62,  "baseline": 64.0, "delta": -2.0, "n": 6},
    "sleep_asleep_min": {"latest": 404, "baseline": 410.5, "delta": -6.5, "n": 6},
    "sleep_eff_pct":    {"latest": 81,  "baseline": 85.0, "delta": -4.0, "n": 6},
    "active_min":       {"latest": 24,  "baseline": null, "delta": null, "n": 1,
                         "why": "baseline needs 3 earlier days with data, has 1"}
  },
  "since_unix": …                        // only on `coach since` output
}
```

**Days without data.** A day with no worn minute is `{"date", "start_unix", "no_data": true}` (plus
`open` while it can change) and nothing else: no `worn_min: 0`, because that would claim the strap was
off. Before `window.data_since_unix` it means the DB has nothing that early. A `no_data` day after it
is covered by a gap in `gaps`. Days with data still report `unworn_min` for strap-off minutes.

**Trends.** `latest` is the last day that has the metric; `n` counts the earlier days in the window
that have it (no_data days and days without a night never count). `baseline` (their mean) and `delta`
are reported only when `n ≥ 3`; otherwise both are `null` and `why` says how many there are. Don't
describe a change from a null baseline.
- `active_min` (also in each day row) = `duration_min` of the workouts starting that day (Will's confirmed minutes
  where confirmed, else detected) + minutes at or above Z1 outside any detected workout window. Today
  can be `latest` while still open (partial); open days never enter the baseline.
- `sleep_*` use the row's `sleep`, which is nights only.

**Sleep: nights and naps.** A session is a night when it is at least 3 h in bed and at least half
of that falls between 20:00 and 11:00 local. Anything else is a nap: it shows in the row only as
`nap_min`, and never in `sleep` or the sleep trends. `coach day` and the daily archive list every
session with `"night": true|false`.

**Workouts** are detected, not logged: runs of minutes at or above Z1 (50% of heart-rate reserve),
merged across dips or dropouts shorter than 3 min, kept when the run spans at least 10 min. A brisk
walk can qualify. Lifting with short sets may not. `max_hr` is a 1-minute mean; `coach workout` adds
`peak_hr_5s` and `peak_hr_1s`. `id` is the detected start in local time, `YYYY-MM-DD-HHMM`; pass it
to `coach workout` and `coach confirm`.

**Gaps** are stretches of 30 min or more with no worn HR between the first and newest data. Time after
the newest sample isn't a gap; it's `freshness.data_age_min`.

## Confirmed durations

The detected window is when HR was elevated, which is not always when the workout was (warm-up,
cool-down, a stop at a light). When Will gives the real duration, Coach records it and it overrides
the detected one everywhere:

```
whoop-poc coach confirm 1794403440 --minutes 20             # start: unix, HH:MM (today) or 2026-11-11T08:24
whoop-poc coach confirm 2026-11-11T08:24 --minutes 20 --start 08:25
whoop-poc coach confirm 2026-11-11T08:24 --clear
```

`<start>` (an `id`, unix, `HH:MM` or ISO time) picks the detected workout as `coach workout` does (containing it, or within 15 min).
Output is `whoop-poc/workout-confirm@1`: `{recorded|cleared: true, workout: {…}, file, rewritten}`.
Exit 1 if there is no workout (or, with `--clear`, no confirmation) there; exit 2 on bad usage.

- Workout fields: `duration_min` is the confirmed minutes when `confirmed`, otherwise the detected
  ones; `detected_min` is always the HR-elevated window; `needs_confirm` is `true` until Will confirms.
  Objects (day files, `coach day`, `coach workout`) also carry `confirmed` and `confirmed_start_unix`
  (`null` unless `--start` was given). `start_unix`, `end_unix` and `local` stay the detected window,
  so `start_unix` still identifies the workout.
- Confirmations live in `~/.local/state/whoop-poc/workout-confirm.json`, not in `whoop.db`, so
  re-detection and archive rewrites don't lose them. Each is matched to the detected workout whose
  start is nearest, within 10 min, so it still applies if detection shifts a little.
- `confirm` rewrites `coach.json` and every `daily/` file it affects at once, final ones included
  (they keep `final: true`). Later syncs apply confirmations the same way.

## Drill-downs

`whoop-poc coach day <date>` → `whoop-poc/day-view@2`:
`{date, tz, final, summary (the day row), sleep: [every session ending that day, same shape as the row's
sleep plus "night": true|false], workouts: [objects with the summary's columns as keys], gaps, hourly: {cols: [start_unix, local,
worn_min, avg_hr, max_hr], rows}, archive: "<path of the day file>"}`.

`whoop-poc coach workout <id>` → `whoop-poc/workout@2`: see [Workout detail](#workout-detail). No match →
exit 1 with `{"error", "workouts": [that day's workouts]}`.

## Workout detail

`whoop-poc coach workout <id> [--kind run|lift] [--series 1m|10s]` → `whoop-poc/workout@2`, about 1 KB.
`<id>` is the summary's `id` (`2026-11-11-0824`); a unix time, `HH:MM` (today) or ISO time also work,
picking the workout whose span contains it or that starts or ends within 15 min of it. Every field is
computed from the strap's per-second HR, so Coach doesn't need finer data. No arrays unless `--series`
asks: `1m` adds `hr_per_minute: [[minute_start_unix, bpm], …]` over the detected window, `10s` adds
`hr_10s: [[bin_start_unix, bpm], …]` through 3 min after it (~2.5 KB more for 20 min).

```jsonc
{
  "schema": "whoop-poc/workout@2", "tz": "America/New_York", "id": "2026-11-11-0824",
  "start_unix": …, "end_unix": …, "local": "11-11 08:24-08:46",   // detected window, as in the summary
  "duration_min": 20, "detected_min": 22, "needs_confirm": false,  // as in Confirmed durations
  "confirmed": true, "confirmed_start_unix": null,
  "avg_hr": 157, "max_hr": 172, "zone_min": {…},   // 1-minute means, as in the summary
  "kind": null,                       // what --kind said: null, "run" or "lift"
  "peak_hr_5s": 182,                  // highest mean over 5 consecutive seconds (≥ 4 readings)
  "peak_hr_1s": 184,                  // highest single reading (one-second spikes can be optical noise)
  "zones": {"max_hr": 190, "rest_hr": 60, "Z1": 125, …, "Z5": 177},
  "z4_plus_min": 4.0,                 // per-second readings at or above Z4's low bound (164), in minutes
  "effort_end_unix": …, "effort_end_local": "08:45:20",
  "recovery": {"end_hr": 148, "drop_1min": 46, "drop_2min": 53},   // + "why" when a drop is null
  "drift": {"steady": false, "resid_bpm": 7.6, "from_unix": …, "to_unix": …, "drift_bpm": null,
            "why": "30 s HR strays 7.6 bpm RMS from a line (> 6)"},
            // steady: + "first_half_hr", "second_half_hr", "drift_bpm", "drift_pct"
  "intervals": [],                    // [] = steady effort; else [{"kind": "work|rest", "start_unix", "local",
                                      //   "sec", "avg_hr", "peak_hr_5s", "hr_drop" (rest only)}]
  "cadence": {"cadence_spm": null, "imu_sec": 0, "why": "no 100 Hz IMU for this workout: …"}
}
```

- **Effort end.** The detected `end_unix` is late: a minute leaves a workout only once its mean falls
  below Z1. `effort_end_unix` is the top of the final HR descent: take the last 10 s bin whose mean is at
  or above the midpoint of Z1's low bound and `avg_hr`, then the highest bin in the 60 s up to it.
  Recovery and the effort's analysis window end there.
- **Recovery.** `end_hr` = mean HR over the 5 s before the effort end; `drop_1min` / `drop_2min` =
  `end_hr` minus the mean over 5 s centred 60 / 120 s after it. Each needs at least 3 readings; with a
  strap gap there it is `null` and `why` says which.
- **Drift** is reported only on a steady effort. Window: 5 min after the start (HR still rising) to the
  effort end, at least 10 min long. **Steady** = no intervals detected and 30 s HR means within 6 bpm RMS
  of a straight line, so a steady climb counts and surges or a wandering pace don't. `drift_bpm` =
  second-half mean HR minus first-half mean; `drift_pct` is that over the first half. Not steady →
  `drift_bpm: null` with `why`.
- **Intervals** come from HR, which lags effort by ~30 s, so segments do too. Over 30 s means up to the
  effort end, an HR reversal of at least 15 bpm separates legs: rising = `work`, falling = `rest`
  (`hr_drop` = the fall in 30 s means). The list is non-empty only when at least 2 rests last 30 s or
  more and are followed by work. The last descent is the cool-down, so one surge doesn't count.
- **`z4_plus_min`** counts seconds, so it can differ from `zone_min`'s Z4+Z5, which counts minutes by their
  mean.
- **Cadence** needs the strap's 100 Hz accelerometer (type-43 `REALTIME_RAW_DATA`), which it streams
  only during a live HR session. History keeps 1 Hz gravity, too slow for steps
  (~2.5–3 Hz). With at least 60 s of IMU in the effort: steps per minute = the autocorrelation peak of
  accel magnitude over 100–220 spm per 10 s window, median over the windows; `imu_sec` says how much
  IMU there was. Not checked against a step count yet.
- **`--kind lift`** replaces `z4_plus_min`, `effort_end_*`, `recovery`, `drift`, `intervals` and
  `cadence` with `lifting: {"sets", "set_sec": […], "rest_sec": […], "total_min", "method"}`, keeping HR
  to avg / peak / duration. Sets = runs where the 5 s mean per-second wrist motion (|Δgravity|, L1) is
  at least 0.15 g, merged across pauses under 8 s, at least 10 s long. 1 Hz gravity only, and there
  are no labelled lifting sessions to tune it on, so `method` says "unvalidated". `sets: null` with
  `why` if gravity covers under half the workout.

## Daily archive: `whoop-poc/day@2`

`~/.local/state/whoop-poc/daily/YYYY-MM-DD.json`, one per local day with data, written by
`whoop-poc json --out` (the service) for every day in the summary window.

```jsonc
{
  "schema": "whoop-poc/day@2", "date": "2026-11-10", "tz": "America/New_York",
  "start_unix": …, "end_unix": …,        // 23 or 25 h on DST change days
  "final": true,                         // the day is over and the strap was drained past its end
  "generated_unix": …, "newest_sample_unix": …,
  "summary": {…},                        // the day row, as in snapshot@3
  "workouts": [{…}],                     // objects, keys = the summary's workout columns
  "gaps": [{…}],
  "sleep": {"source": "noop|heuristic", "sessions": [{   // sessions ending this day
    "start_unix", "end_unix", "start_iso", "end_iso", "in_bed_min", "asleep_min", "stage_min",
    "avg_hr", "resting_hr", "avg_hrv_ms", "night",                            // night: see Sleep: nights and naps
    "stages": [{"start_unix", "end_unix", "stage": "light|rem|deep|wake"}]}]},   // stages: NOOP only
  "labels": [{"start_unix", "end_unix", "activity"}],
  "zones": {…},                          // as in the summary: the bounds zone_min was computed with
  "hr_per_minute": [[minute_start_unix, mean_bpm], …]                          // worn minutes only
}
```

- A file is rewritten while its day is not `final` and its content changes. Once a file marked `final`
  exists, it is rewritten only when its `zones` bounds differ from the ones in use (see "Max HR") or a
  confirmation changes its workouts. A day that becomes final while out of the summary window stays as last
  written, with `final: false`.
- Files older than `WHOOP_ARCHIVE_DAYS` (default 30) days are deleted on each write.
- Days with no data at all get no file.

## Size

The summary carries what Coach needs for common questions (how did I sleep, how active was I on a
given day, is resting HR drifting, did the strap sync) without a drill-down, and nothing per-minute.
Soft target: **about 3.5 KB for the default 7-day window** with a workout, a night and full wear
every day (3.1 KB measured), plus about 330 B for each extra day of `WHOOP_COACH_DAYS`. Real weeks
with less wear or fewer workouts are smaller (2.0 KB on 2026-11-11). A test fails above 3.5 KiB for
the full synthetic week; a field added to every day row should earn its bytes by answering a common
question.

## Versions

- **`workout@2`** (2026-11-11). `coach workout` returns the computed detail above. Removed:
  `hr_per_minute` by default (now `--series 1m`). Added to snapshot@3's workout table and every workout
  object: `id` (the last column).
- **`snapshot@3`, `day@2`, `day-view@2`** (2026-11-11). Meaning changes: a day with no worn minute
  is `no_data: true` with no `worn_min` (was `worn_min: 0`); the row's `sleep` and the sleep trends
  are nights only (naps move to `nap_min`); trend `n` counts earlier days with data and `baseline` /
  `delta` are `null` below 3; `active_min` counts workout `duration_min` plus Z1+ minutes outside
  workouts, and today can be its `latest`. Added: row `active_min`, `nap_min`, `freshness.hint`,
  trend `why`, session `night`. Archive files marked `final` before this keep `day@1` and its
  meanings until they age out after 30 days; check `schema` per file.
- `snapshot@2`, `day@1`, `day-view@1`: the previous versions.

## Per-minute snapshot: `whoop-poc/snapshot@1`

The previous `coach.json` format, still available from `whoop-poc json --raw [--hours 24]` and
`GET /v1/snapshot?hours=24` for programs that want every minute: `window`, `device`, `freshness`
(`newest_sample_unix`, `data_age_min`, `last_received_unix`, `last_sync_attempt`), `hr` (`latest`,
`stats`, `per_minute: [[minute_start_unix, mean_bpm], …]`), `sleep` (`source`, `sessions` as in the day
file), `battery`, `labels`. Too large for an LLM's context; don't hand it to Coach.

## Settings

In `~/.config/whoop-poc/env` (or the environment):

| Key | Default | Meaning |
|---|---|---|
| `WHOOP_COACH_DAYS` | 7 | summary window, calendar days including today (`--days` overrides) |
| `WHOOP_ARCHIVE_DAYS` | 30 | daily files kept; also how far back `coach since` reaches |
| `WHOOP_TZ` | `America/New_York` | time zone for day boundaries and `local` strings |
| `WHOOP_MAX_HR` | unset | max HR override (`--max-hr` beats it); unset = observed peak if above 190, else 190 (see "Max HR") |
| `WHOOP_RESTING_HR` | 60 | resting HR for the heart-rate reserve |

## Reading it well

- **Is it fresh?** `freshness.data_age_min`, then `last_sync.outcome`. `not_advertising` means the strap
  wasn't reachable (out of range, phone holding it, asleep); with stale data `freshness.hint` says so
  in one line, naming the sync host. `partial` means a drain stopped early, and
  the next run resumes it with nothing lost. A recent `complete` with old data usually means the strap
  isn't being worn.
- **Sleep is approximate.** NOOP's stager agrees with lab staging roughly 65–73% per 30 s epoch at best,
  and deep sleep is the least reliable stage. The heuristic fallback (`sleep_source: "heuristic"`) counts
  lying still as asleep. A night can come back as two sessions if there's a long wake in the middle. A
  session only appears after the morning sync has drained the night.
- HR is per-second strap history averaged per minute. It's not live: the newest value is as old as the
  last sync.

## Not (yet) here

Recovery/strain scores, SpO₂, skin temperature (unmapped in NOOP's Linux path), and a push/subscribe
channel. Poll instead: `coach.json`'s mtime changes after every sync attempt.

# Reading WHOOP data from another program (Coach)

Coach is an LLM assistant, so what it reads by default is a **summary**: one row per day, detected
workouts, data gaps, freshness and trends. It is under ~3 KB for a full week. Per-minute HR and sleep
stage segments stay in `whoop.db` and the daily archive, and Coach asks for them one small piece at a
time. Never scrape the TUI or HTML.

| What | How | Size |
|---|---|---|
| **Summary** (`whoop-poc/snapshot@2`) | file `~/.local/state/whoop-poc/coach.json`, or `whoop-poc coach`, or `GET http://127.0.0.1:8787/v1/coach` | ≤ ~3 KB |
| New since a cursor | `whoop-poc coach since <cursor_unix>`, or `GET /v1/coach?since=<unix>` | smaller |
| One day | `whoop-poc coach day 2026-11-11` (also `today`, `yesterday`) | ~1–2 KB |
| One workout, 1-min HR | `whoop-poc coach workout <start_unix \| HH:MM \| 2026-11-11T08:24>` | < 1 KB for 20 min |
| Record Will's duration | `whoop-poc coach confirm <start> --minutes N [--start HH:MM]`; `--clear` removes it | < 1 KB |
| Daily archive (`whoop-poc/day@1`) | `~/.local/state/whoop-poc/daily/YYYY-MM-DD.json` | ~15–25 KB per day |
| Per-minute snapshot (`whoop-poc/snapshot@1`) | `whoop-poc json --raw [--hours 24]`, `GET /v1/snapshot?hours=24` | ~45 KB per day |

`whoop-sync.service` refreshes `coach.json` and the archive after every sync attempt (`ExecStopPost`
runs `whoop-poc json --out coach.json`), roughly every 15–35 min. Files are written atomically
(tmp + rename), mode 600, in a 700 directory. It is biometric data: it lives outside the repo and is
never committed. All CLI drill-downs print one line of compact JSON to stdout.

## Recommended read flow

1. **Read the summary.** Read `coach.json` (or run `whoop-poc coach`). Check `freshness` before
   believing anything: a large `data_age_min` means the strap hasn't synced (see `last_sync.outcome`).
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
   sleep sessions, `coach workout <start_unix>` for a workout's minute-by-minute HR.

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
  Z1–Z5 at 50/60/70/80/90%. `zones` in the summary gives the bpm bounds in use.

## Summary: `whoop-poc/snapshot@2`

```jsonc
{
  "schema": "whoop-poc/snapshot@2", "demo": false,
  "generated_unix": 1794408430, "tz": "America/New_York",
  "window": {"since_unix": …,          // local midnight, `days` calendar days ago incl. today
             "days": 7,                  // WHOOP_COACH_DAYS
             "data_since_unix": …},      // first worn minute in the window (earlier = no data at all)
  "cursor_unix": 1794408217,             // newest sample; next time call `coach since <this>`
  "freshness": {"data_age_min": 3,       // now − newest sample
                "last_sync": {"outcome": "complete|partial|not_advertising|error", "finished_unix": …},
                "battery_pct": 44},
  "latest_hr": {"unix": …, "bpm": 96},
  "zones": {"max_hr": 190, "rest_hr": 60, "Z1": 125, "Z2": 138, "Z3": 151, "Z4": 164, "Z5": 177},
  "sleep_source": "noop|heuristic",
  "days": [{                             // one per local day, oldest first
    "date": "2026-11-11", "start_unix": …,
    "open": true,                        // present while the day can still change (not over, or not drained past its end)
    "worn_min": 584,                     // minutes with HR
    "unworn_min": 19,                    // minutes without HR between the first and newest data (strap off)
    "rest_hr": 62,                       // ~5th pct of still minutes; only with ≥ 120 worn min
    "avg_hr": 76, "max_hr": 172,
    "zone_min": {"Z1": 5, "Z3": 14},     // minutes per zone, zeros left out
    "sleep": {                           // the longest session ending this day (last night belongs to today)
      "start_unix": …, "end_unix": …, "local": "23:59-08:16",   // in bed = start..end
      "asleep_min": 404, "eff_pct": 81,
      "resting_hr": 49,                  // NOOP stager; the heuristic gives "avg_hr" (mean asleep HR) instead
      "stage_min": {"wake": 11, "rem": 40, "light": 150, "deep": 47}},   // NOOP stager only
    "sleep_sessions": 2                  // present when more than one session ended this day
  }],
  "workouts": {"cols": ["start_unix", "end_unix", "local", "duration_min", "avg_hr", "max_hr", "zone_min",
                        "detected_min", "needs_confirm"],
               "rows": [[1794403440, 1794404760, "11-11 08:24-08:46", 20, 157, 172, {"Z1": 2, "Z2": 2, "Z3": 14, "Z4": 4},
                         22, false]]},   // duration_min = Will's 20; detected_min = the HR window, 22
  "gaps": [{"start_unix": …, "end_unix": …, "local": "13:05-15:40", "min": 155}],   // ≥ 30 min without worn data
  "trends": {                            // latest value vs the mean of the earlier days that have it
    "rest_hr":          {"latest": 62,  "baseline": 64.0, "delta": -2.0, "n": 6},
    "sleep_asleep_min": {"latest": 404, "baseline": 410.5, "delta": -6.5, "n": 6},
    "sleep_eff_pct":    {"latest": 81,  "baseline": 85.0, "delta": -4.0, "n": 6},
    "active_min":       {"latest": 40,  "baseline": 31.2, "delta": 8.8, "n": 5}   // Z1+ minutes, complete days only
  },
  "since_unix": …                        // only on `coach since` output
}
```

**Workouts** are detected, not logged: runs of minutes at or above Z1 (50% of heart-rate reserve),
merged across dips or dropouts shorter than 3 min, kept when the run spans at least 10 min. A brisk
walk can qualify. Lifting with short sets may not. `max_hr` is a 1-minute mean; `coach workout` adds
`peak_hr_1s`, the highest per-second reading.

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

`<start>` picks the detected workout as `coach workout` does (containing it, or within 15 min).
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

`whoop-poc coach day <date>` → `whoop-poc/day-view@1`:
`{date, tz, final, summary (the day row), sleep: [every session ending that day, same shape as the row's
sleep], workouts: [objects with the summary's columns as keys], gaps, hourly: {cols: [start_unix, local,
worn_min, avg_hr, max_hr], rows}, archive: "<path of the day file>"}`.

`whoop-poc coach workout <start>` → `whoop-poc/workout@1`: the workout whose span contains `<start>`
or starts or ends within 15 min of it. Fields are the summary's workout columns plus `peak_hr_1s`,
`zones` and `hr_per_minute: [[minute_start_unix, bpm], …]` for the workout only. No match → exit 1 with
`{"error", "workouts": [that day's workouts]}`.

## Daily archive: `whoop-poc/day@1`

`~/.local/state/whoop-poc/daily/YYYY-MM-DD.json`, one per local day with data, written by
`whoop-poc json --out` (the service) for every day in the summary window.

```jsonc
{
  "schema": "whoop-poc/day@1", "date": "2026-11-10", "tz": "America/New_York",
  "start_unix": …, "end_unix": …,        // 23 or 25 h on DST change days
  "final": true,                         // the day is over and the strap was drained past its end
  "generated_unix": …, "newest_sample_unix": …,
  "summary": {…},                        // the day row, as in snapshot@2
  "workouts": [{…}],                     // objects, keys = the summary's workout columns
  "gaps": [{…}],
  "sleep": {"source": "noop|heuristic", "sessions": [{   // sessions ending this day
    "start_unix", "end_unix", "start_iso", "end_iso", "in_bed_min", "asleep_min", "stage_min",
    "avg_hr", "resting_hr", "avg_hrv_ms",
    "stages": [{"start_unix", "end_unix", "stage": "light|rem|deep|wake"}]}]},   // stages: NOOP only
  "labels": [{"start_unix", "end_unix", "activity"}],
  "hr_per_minute": [[minute_start_unix, mean_bpm], …]                          // worn minutes only
}
```

- A file is rewritten while its day is not `final` and its content changes. Once a file marked `final`
  exists, it is never rewritten. A day that becomes final while out of the summary window stays as last
  written, with `final: false`.
- Files older than `WHOOP_ARCHIVE_DAYS` (default 30) days are deleted on each write.
- Days with no data at all get no file.

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
| `WHOOP_MAX_HR`, `WHOOP_RESTING_HR` | 190, 60 | heart-rate reserve for zones and workout detection |

## Reading it well

- **Is it fresh?** `freshness.data_age_min`, then `last_sync.outcome`. `not_advertising` means the strap
  wasn't reachable (out of range, phone holding it, asleep). `partial` means a drain stopped early, and
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

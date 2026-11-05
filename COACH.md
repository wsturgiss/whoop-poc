# Reading WHOOP data from another program (Coach)

Other local programs should read one of the two surfaces below and never scrape the TUI or HTML. Both
return the same JSON document, `whoop-poc/snapshot@1`.

| Surface | How | When it updates |
|---|---|---|
| **File** | `~/.local/state/whoop-poc/coach.json` (mode 600) | After every `whoop-sync.service` attempt (`ExecStopPost`), roughly every 15–35 min |
| **CLI** | `whoop-poc json [--hours 24]` → stdout | On demand |
| **HTTP** | `whoop-poc serve [--port 8787]` → `GET http://127.0.0.1:8787/v1/snapshot?hours=24` | On demand (localhost only, no auth) |

The file is written atomically (tmp + rename), so a reader never sees a half-written document. It is
biometric data: it lives outside the repo and is never committed.

## Contract

- `schema` is `"whoop-poc/snapshot@1"`. Within `@1`, fields are only ever **added**. A rename,
  removal or meaning change bumps it to `@2`. Readers should check the prefix and ignore unknown keys.
- All times are unix seconds (`*_unix`). `*_iso` fields are the same instants in the generating machine's
  local zone, with the offset included.
- `null` means "not known", never zero.
- `demo: true` marks synthetic data (`--demo`). Never treat it as the wearer's.

```jsonc
{
  "schema": "whoop-poc/snapshot@1",
  "demo": false,
  "generated_unix": 1793860647, "generated_iso": "2026-11-05T01:37:27-05:00",
  "window": {"since_unix": …, "until_unix": …, "hours": 24.0},
  "device": {"address": "AA:BB:…", "name": "WHOOP 4C…"},

  "freshness": {
    "newest_sample_unix": …,          // newest decoded HR second in the window
    "newest_sample_iso": "…",
    "data_age_min": 87.8,             // now − newest sample; large = strap not syncing or not worn
    "last_received_unix": …,          // last time any frame arrived over BLE
    "last_sync_attempt": {            // from whoop-sync's sync.json; null if never recorded
      "schema": 1, "attempt_unix": …, "finished_unix": …,
      "outcome": "complete|partial|not_advertising|error|unknown",
      "exit": 0, "frames_stored": 2261
    }
  },

  "hr": {
    "latest": {"unix": …, "iso": "…", "bpm": 63},           // null if no worn samples in window
    "stats": {"min": 60, "avg": 72.3, "max": 106,             // over worn minutes in window
              "resting_est": 61, "worn_min": 44},              // resting_est ≈ 5th pct of still minutes
    "per_minute": [[minute_start_unix, mean_bpm], …]          // worn minutes only, ascending
  },

  "sleep": {
    "source": "noop|heuristic",       // noop = NOOP SleepStager (4 stages); heuristic = motion+HR fallback
    "sessions": [{
      "start_unix": …, "end_unix": …, "start_iso": "…", "end_iso": "…",
      "in_bed_min": 248, "asleep_min": 237,
      "stage_min": {"wake": 11, "rem": 40, "light": 150, "deep": 47},   // null for heuristic
      "avg_hr": 53.5, "resting_hr": 49, "avg_hrv_ms": 57.4,              // resting/HRV: noop only
      "stages": [{"start_unix": …, "end_unix": …, "stage": "light|rem|deep|wake"}, …]   // null for heuristic
    }]
  },

  "labels": [{"start_unix": …, "end_unix": …, "activity": "awake"}]     // ground truth from `whoop_sync.py label`
}
```

## Reading it well

- **Is it fresh?** Check `freshness.data_age_min`, and if it is large, `last_sync_attempt.outcome`.
  `not_advertising` means the strap wasn't reachable (out of range, phone holding it, asleep). `partial`
  means a drain stopped early, and the next run resumes it with nothing lost. A recent `complete` with an
  old sample usually means the strap isn't being worn.
- **Sleep is approximate.** NOOP's stager agrees with lab staging roughly 65–73% per 30 s epoch at best,
  and deep sleep is the least reliable stage. A night can come back as two sessions if there's a long
  wake in the middle. A session only appears after the morning sync has drained the night.
- HR is per-second strap history averaged per minute. It's not live: the newest value is as old as the
  last sync.

## Not (yet) here

Recovery/strain scores, SpO₂, skin temperature (unmapped in NOOP's Linux path), and a push/subscribe
channel. Poll the file instead: its mtime changes after every sync attempt.

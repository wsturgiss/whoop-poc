# whoop-poc local API

A small HTTP JSON API for whoop-poc front ends: the Textual TUI now, an Omarchy bar widget / overlay
later. HTTP rather than a unix socket because QML's `XMLHttpRequest` speaks HTTP only.

`coach.json` stays the Coach contract ([COACH.md](COACH.md)); `/v1/summary` serves the same document,
built fresh, as an additional reader.

## Running it

```bash
whoop-poc serve                    # http://127.0.0.1:8787/v1/
whoop-poc serve --port 8790
systemctl --user start whoop-api   # the unit below
```

- **Bind:** `127.0.0.1` only. `--host` accepts only `127.0.0.1` or `localhost`; anything else
  (`0.0.0.0`, `::`, a LAN address) refuses to start.
- **Port:** `--port` if passed, else `WHOOP_API_PORT` (environment, then `~/.config/whoop-poc/env`),
  else **8787**. The TUI resolves its port the same way.
- **Auth:** none beyond the loopback bind. Two checks keep other origins out:
  - the `Host` header must be `127.0.0.1` or `localhost` (with any port), otherwise `403`; this blocks
    DNS rebinding;
  - a POST must send `Content-Type: application/json`, otherwise `415`. A web page on another origin
    can't send that without a CORS preflight, and the server sends no CORS headers, so a browser tab
    can't start a sync or a live session.
- **Logging:** to stderr (the journal under systemd), one line per POST, per error and per heavy read
  (report, summary, snapshot), with method, path, status and time. The status and live polls aren't
  logged. Query strings, bodies and config values never are.
- **Data:** whoop.db is opened read-only. Syncs and live sessions the API starts run detached, exactly as
  from the TUI. Heavy reads (report, summary, snapshot) run in one worker process, so `/v1/status` and
  `/v1/live` answer immediately while a report builds.

### systemd user unit

`contrib/systemd/whoop-api.service` (`Type=simple`, `ExecStart=%h/.local/bin/whoop-poc serve`,
`Restart=on-failure`, no timer). It is opt-in:

```bash
contrib/install.sh --api                  # or: install -m 644 contrib/systemd/whoop-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start whoop-api          # this session only
systemctl --user enable --now whoop-api   # also at login
journalctl --user -u whoop-api -n 20
```

`whoop-sync.{service,timer}` are independent of it.

## Conventions

- Paths are versioned under `/v1/`; documents carry a `schema` (`whoop-poc/<name>@<n>`). Within a
  version fields are only ever added (same rule as COACH.md).
- Responses are compact JSON (`Content-Type: application/json`, `Cache-Control: no-store`).
- Errors: `{"error": "<message>"}` with `400` (bad query or body), `403` (Host), `404` (no such path),
  `405` (wrong method), `409` (the action doesn't apply now, e.g. extending a live session that isn't on),
  `415` (POST without JSON), `500` (something actually broke).
- Times are unix seconds (`*_unix`). HR is bpm.

## Reads (GET)

| Path | Purpose |
|---|---|
| `/healthz`, `/v1/health` | liveness: `{"ok": true, "demo": false}` |
| `/v1/status` | freshness, battery, sync and live state; cheap, poll it every second |
| `/v1/summary[?since=UNIX]` | the Coach summary, `whoop-poc/snapshot@3` (COACH.md); `/v1/coach` is the same |
| `/v1/report?hours=N[&bins=N][&series=minute]` | what the TUI window shows |
| `/v1/live[?session=UNIX&since=UNIX]` | live session state and its samples |
| `/v1/snapshot?hours=N` | the old per-minute `whoop-poc/snapshot@1` (large; not for widgets) |

### `/v1/status` (`whoop-poc/status@1`)

```jsonc
{"schema": "whoop-poc/status@1", "now_unix": …, "demo": false,
 "data": {"newest_unix": …, "age_min": 3},          // newest history frame in whoop.db
 "battery": {"pct": 68, "unix": …} | null,          // last strap battery read (battery.json)
 "sync": {"running": false,
          "progress": null | {"phase": "connecting|draining|reconnecting|finishing",
                              "pct", "at", "rate", "eta", "stored"},   // while a drain runs
          "last": {"outcome": "complete|partial|not_advertising|error", "finished_unix", "age_min"} | null},
 "live": null | {"state": "connecting|waiting|live|reconnecting", "pid", "bpm", "unix", "until_unix", …}}
```

### `/v1/report` (`whoop-poc/report@1`)

`hours` 0.1–744 (default 24). `bins` 10–4000 (default 480).

```jsonc
{"schema": "whoop-poc/report@1", "demo": false,
 "device": {"name", "address"},
 "window": {"since_unix", "until_unix", "hours"}, "newest_unix": …,
 "hr": {"latest": {"unix", "bpm"} | null,
        "stats": {"min", "avg", "max", "rest", "worn_min"} | null,   // over worn 1-minute means
        "zone_sec": {"Z0": …, "Z1": …, …, "Z5": …},                   // worn minutes × 60 per zone
        "series": {"agg": "max", "bins": 480, "points": [[unix, bpm], …]}},
 "zones": {…},          // as in the summary: bounds, max_hr_source, observed_peak (COACH.md, "Max HR")
 "max_hr": {"max_hr": 190, "source": "override|observed|default", "observed": {…} | null,
            "text": "max 190 (default; observed 5-s peak 182, 11-11 08:42:08 in 2026-11-11-0824)"},
 "sleep": {"source": "noop|heuristic", "note": "<why there is none>" | null,
           "sessions": [{"start_unix", "end_unix", "in_bed_min", "asleep_min", "avg_hr", "low_hr",
                         "stage_min": {"deep", "light", "rem", "wake"} | null, "resting_hr", "avg_hrv_ms"}],
           "last": {"start_unix", "end_unix",
                    "stages": [[start_unix, end_unix, "light|rem|deep|wake"], …] | null,   // NOOP stager
                    "minutes": "1110-0…" | null}},   // heuristic: per minute 1 asleep, 0 not, - no data
 "workouts": [{"id", "start_unix", "end_unix", "duration_min", "detected_min", "confirmed", "avg_hr",
               "max_hr", "peak_hr_5s", "zone_min", "recovery"}]}     // oldest first (coach workout rows)
```

**The HR series is downsampled, never per-second.** The window `[since, until]` is cut into `bins`
equal slices, and each slice gives its peak minute: the highest 1-minute mean, at that minute's real time.
Slices with no worn minute (strap off) are left out. The slicing is the same as the TUI chart's column
slicing, so a chart `bins` columns wide that draws the peak per column comes out exactly as it would from
every minute (the TUI passes its column count). A 24 h report is about 9 KB. `series=minute` returns
every worn minute's mean instead (`agg: "mean"`, `bins: null`; ~25 KB/day).

### `/v1/live` (`whoop-poc/live@1`)

```jsonc
{"schema": "whoop-poc/live@1",
 "running": {…} | null,          // live.json while its process is alive (as status.live)
 "last": {…} | null,             // live.json whatever its state, e.g. "ended" (the TUI reviews it)
 "session_start_unix": … | null, // the first sample's time: identifies the session
 "full": true,
 "samples": [[unix, bpm], …]}    // this session's 1 Hz samples
```

To poll cheaply, pass back `session=<session_start_unix>&since=<newest sample unix you have>`. If the
session is still that one you get `"full": false` and only the newer samples. Otherwise (a new session)
you get `"full": true` and all of them.

## Actions (POST)

Send `Content-Type: application/json`; the body is optional unless noted. Each one is safe to repeat:
if the thing is already in the requested state you get `200` with the current state, not an error.

| Path | Body | Result |
|---|---|---|
| `/v1/sync` | — | `202 {"started": true, "message"}`; already draining: `200 {"started": false, "message", "progress"}`; can't start (no `WHOOP_ADDRESS` / wrapper): `500` |
| `/v1/live/start` | `{"minutes": 1–180}` (default `WHOOP_LIVE_MINUTES`, 30) | `202 {"started": true, "message", "minutes"}`; already on: `200 {"started": false, "message", "running"}` |
| `/v1/live/extend` | — | `200 {"extended": true, "minutes": 10}` (3 h cap); not on: `409` |
| `/v1/live/stop` | — | `200 {"stopped": true|false}` (false = it wasn't on) |

`/v1/sync` does what the TUI's `s` key does: it starts `whoop-sync.service` (or a detached
`whoop-sync sync`), which the caller doesn't own. `/v1/live/start` pauses a running drain first, as `l` does.

```bash
curl -s 127.0.0.1:8787/v1/status
curl -s '127.0.0.1:8787/v1/report?hours=24&bins=200'
curl -s -X POST -H 'Content-Type: application/json' 127.0.0.1:8787/v1/sync
curl -s -X POST -H 'Content-Type: application/json' -d '{"minutes":15}' 127.0.0.1:8787/v1/live/start
```

## One code path: `api.dispatch`, `client.py`, and the fallback

Every endpoint is a handler in `api.py`'s `ROUTES` table, and `api.dispatch(cfg, method, path, query,
body)` → `(status, document)` is the only way in. The HTTP server calls it for each request.

`client.py`'s `Client` is what the TUI uses:

1. It sends the request to `http://127.0.0.1:<port>`: a 1.5 s timeout for polls and actions, 120 s for
   report and summary builds.
2. If nothing answers (connection refused, timeout), it calls `api.dispatch` **in-process** (heavy routes
   in the TUI's worker process) and keeps doing that for 15 s before trying the server again. So the TUI
   works the same with or without the server, and picks the server up within 15 s of it starting.
3. An error status from a running server is not a reason to fall back: it raises `api.ApiError(code,
   message)`, which the in-process path raises too.

In-process results are round-tripped through JSON, so both paths hand the caller identical types. The
TUI uses the server only for the configuration the server has: a `--demo`, `--db`, `--address` or
`--max-hr` run stays in-process. While it talks to the server, its top line ends with `via API :8787`.

A QML widget mirrors steps 1–2 without the fallback: poll `/v1/status` (and `/v1/report?hours=24&bins=<px>`
less often), and show "whoop-poc API not running" when the connection fails.

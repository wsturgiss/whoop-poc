"""Local HTTP JSON API for whoop-poc front ends: the Textual TUI now, an Omarchy bar widget later.

`whoop-poc serve` binds 127.0.0.1 only (port: --port, else WHOOP_API_PORT, else 8787). Every request goes
through dispatch(), and client.py's in-process fallback calls the same dispatch(), so a front end gets
the same documents whether the server is running or not. Endpoints and shapes are in API.md.

Requests are local-only: the Host header must name 127.0.0.1 or localhost (blocks DNS rebinding), and a
POST must send Content-Type: application/json (a cross-site page can't, without a CORS preflight this
server never answers). There is no other auth.
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime

import coach
import whoop_poc as w

HOST = "127.0.0.1"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
DEFAULT_PORT = 8787
REPORT_SCHEMA = "whoop-poc/report@1"
STATUS_SCHEMA = "whoop-poc/status@1"
LIVE_SCHEMA = "whoop-poc/live@1"
REPORT_BINS = 480          # /v1/report HR series: peaks of this many slices by default ...
MAX_BINS = 4000            # ... and at most this many when asked (?bins=, a chart's column count)
MAX_HOURS = 24 * 31
LIVE_MAX_MINUTES = 180     # whoop_live.py caps a session at 3 h


class ApiError(Exception):
    """An error with its HTTP status; the document is {"error": message}."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def port(args=None) -> int:
    """--port if given, else WHOOP_API_PORT (environment or ~/.config/whoop-poc/env), else 8787."""
    import os
    p = getattr(args, "port", None)
    if p is not None:
        return int(p)
    v = os.environ.get("WHOOP_API_PORT", "").strip()
    if not v:
        return DEFAULT_PORT
    if not v.isdigit() or not 0 < int(v) < 65536:
        raise SystemExit("WHOOP_API_PORT must be a port number")
    return int(v)


def _num(q: dict, key: str, default, lo, hi, cast=float):
    if key not in q:
        return default
    try:
        v = cast(q[key])
    except ValueError:
        raise ApiError(400, f"{key} must be a number") from None
    if not (lo <= v <= hi) or (isinstance(v, float) and math.isnan(v)):
        raise ApiError(400, f"{key} must be between {lo} and {hi}")
    return v


# --------------------------------------------------------------------------------------- reads
def health(cfg, q: dict, body: dict) -> tuple[int, dict]:
    return 200, {"ok": True, "demo": bool(cfg.demo)}


def status(cfg, q: dict, body: dict) -> tuple[int, dict]:
    """Freshness, battery, sync and live state: cheap enough to poll every second."""
    now = int(time.time())
    if cfg.demo:
        return 200, {"schema": STATUS_SCHEMA, "now_unix": now, "demo": True, "data": None, "battery": None,
                     "sync": {"running": False, "progress": None, "last": None}, "live": None}
    newest = w.newest_unix(cfg.db, cfg.address)
    last = w.last_sync_attempt()
    progress = w.drain_status()
    return 200, {
        "schema": STATUS_SCHEMA, "now_unix": now, "demo": False,
        "data": {"newest_unix": newest, "age_min": None if newest is None else (now - newest) // 60},
        "battery": w.last_battery(),
        "sync": {"running": progress is not None, "progress": progress,
                 "last": None if not last else {"outcome": last.get("outcome"),
                                                "finished_unix": last.get("finished_unix"),
                                                "age_min": None if not last.get("finished_unix")
                                                else (now - last["finished_unix"]) // 60}},
        "live": w.live_status(),
    }


def summary(cfg, q: dict, body: dict) -> tuple[int, dict]:
    """The coach summary (whoop-poc/snapshot@3, COACH.md); ?since=UNIX as `coach since`."""
    try:
        doc, code = coach.run(cfg, ["since", q["since"]] if "since" in q else [])
    except (ValueError, SystemExit) as e:
        raise ApiError(400, str(e)) from None
    if code:
        raise ApiError(400, doc.get("error", "bad request"))
    return 200, doc


def snapshot(cfg, q: dict, body: dict) -> tuple[int, dict]:
    """The per-minute snapshot@1 (?hours=N, default 24)."""
    hours = _num(q, "hours", 24.0, 0.1, MAX_HOURS)
    return 200, w.snapshot(w.build_report(cfg.db, hours, cfg.address, demo=cfg.demo, heuristic=cfg.heuristic))


def hr_series(r: w.Report, bins: int, per_minute: bool) -> dict:
    """per_minute: every worn minute's mean. Otherwise the peak minute of each of `bins` equal slices of
    [since, until], sliced the way w.zone_chart slices a chart into columns, each point at its minute's
    real time: a chart `bins` columns wide drawing the peak per column gets exactly what it would from
    every minute. Empty slices (strap off) are left out."""
    hm = [m for m in r.hr_minutes if r.since <= m.t <= r.until]
    if per_minute:
        pts = [(m.t, m.hr) for m in hm]
    else:
        span, best = r.until - r.since + 1, {}
        for m in hm:
            c = min(bins - 1, int((m.t - r.since) * bins / span))
            if c not in best or m.hr > best[c][1]:
                best[c] = (m.t, m.hr)
        pts = [best[c] for c in sorted(best)]
    return {"agg": "mean" if per_minute else "max", "bins": None if per_minute else bins,
            "points": [[t, round(v, 4)] for t, v in pts]}


def sleep_doc(r: w.Report) -> dict:
    """The sessions for the TUI's sleep table, and the newest one's timeline (stages, or one character per
    minute: 1 still + calm, 0 not, - no data)."""
    out = {"source": r.meta.get("sleep_source", "heuristic"), "note": None if r.sleep else w.no_sleep_msg(r),
           "sessions": [], "last": None}
    for s in r.sleep:
        hrs = s.hrs
        out["sessions"].append({
            "start_unix": s.start, "end_unix": s.end, "in_bed_min": s.span_min, "asleep_min": s.asleep_min,
            "avg_hr": round(sum(hrs) / len(hrs), 2) if hrs else None, "low_hr": round(min(hrs), 2) if hrs else None,
            "stage_min": s.stage_minutes() if s.stages is not None else None,
            "resting_hr": s.resting_hr, "avg_hrv_ms": round(s.avg_hrv, 2) if s.avg_hrv else None})
    if r.sleep:
        s = r.sleep[-1]
        flags = {m.t: m.asleep for m in s.minutes}
        out["last"] = {"start_unix": s.start, "end_unix": s.end,
                       "stages": None if s.stages is None else [[g["start"], g["end"], g["stage"]] for g in s.stages],
                       "minutes": None if s.stages is not None else "".join(
                           "-" if t not in flags else "1" if flags[t] else "0" for t in range(s.start, s.end, 60))}
    return out


def sleep_window(last: dict) -> w.SleepWindow:
    """sleep_doc's "last" back as a SleepWindow, for stage_bar / bucket / sleep_hours."""
    if last["stages"] is not None:
        return w.SleepWindow(last["start_unix"], last["end_unix"], [],
                             stages=[{"start": a, "end": b, "stage": g} for a, b, g in last["stages"]])
    mins = [w.Minute(last["start_unix"] + 60 * i, None, 0.0, 0, c == "1")
            for i, c in enumerate(last["minutes"]) if c != "-"]
    return w.SleepWindow(last["start_unix"], last["end_unix"], mins)


def report_doc(r: w.Report, bins: int = REPORT_BINS, per_minute: bool = False) -> dict:
    """What the TUI window shows, from coach.report_with_workouts' Report."""
    stats, now = r.hr_stats(), r.hr_now
    zone_sec = {z[0]: 0 for z in (w.REST_ZONE,) + w.ZONES}
    for m in r.hr_minutes:
        zone_sec[w.hr_zone(m.hr)[0]] += 60
    info = w.max_hr_info()
    return {
        "schema": REPORT_SCHEMA, "demo": r.meta.get("address") == "-",
        "device": {"name": r.meta.get("name"), "address": r.meta.get("address")},
        "window": {"since_unix": r.since, "until_unix": r.until, "hours": round((r.until - r.since) / 3600, 2)},
        "newest_unix": r.meta.get("newest_unix"),
        "hr": {"latest": None if now is None else {"unix": now.unix, "bpm": now.hr},
               "stats": None if not stats else {k: round(v, 2) for k, v in stats.items()},
               "zone_sec": zone_sec, "series": hr_series(r, bins, per_minute)},
        "zones": coach.zones(),
        "max_hr": {**info, "text": w.max_hr_text(info)},
        "sleep": sleep_doc(r),
        "workouts": r.meta.get("workouts", []),
    }


def report(cfg, q: dict, body: dict) -> tuple[int, dict]:
    """?hours=N (default 24), ?bins=N HR slices (default 480; pass the chart's column count), ?series=minute
    for every minute's mean instead."""
    hours = _num(q, "hours", 24.0, 0.1, MAX_HOURS)
    bins = _num(q, "bins", REPORT_BINS, 10, MAX_BINS, int)
    if q.get("series", "bins") not in ("bins", "minute"):
        raise ApiError(400, "series is bins or minute")
    r = coach.report_with_workouts(cfg.db, hours, cfg.address, demo=cfg.demo, heuristic=cfg.heuristic)
    return 200, report_doc(r, bins, q.get("series") == "minute")


def live(cfg, q: dict, body: dict) -> tuple[int, dict]:
    """The running session (live.json while its process lives, else null), the last session whatever its
    state, and its samples. ?session=<session_start_unix>&since=UNIX returns only newer samples when
    the session is still that one ("full": false); otherwise all of them ("full": true)."""
    if cfg.demo:
        return 200, {"schema": LIVE_SCHEMA, "running": None, "last": None, "session_start_unix": None,
                     "full": True, "samples": []}
    s = w.live_samples()
    first = s[0][0] if s else None
    session = _num(q, "session", None, 0, 2 ** 40, int)
    since = _num(q, "since", None, 0, 2 ** 40, int)
    incremental = session is not None and since is not None and session == first
    return 200, {"schema": LIVE_SCHEMA, "running": w.live_status(), "last": w.last_live(),
                 "session_start_unix": first, "full": not incremental,
                 "samples": [[t, b] for t, b in s if t > since] if incremental else [[t, b] for t, b in s]}


# -------------------------------------------------------------------------------------- actions
def sync_start(cfg, q: dict, body: dict) -> tuple[int, dict]:
    """Start a background drain (as the TUI's s key). Already running → 200 with started false."""
    if w.drain_running():
        return 200, {"started": False, "message": "A drain is already running", "progress": w.drain_status()}
    ok, msg = w.start_sync_background()
    if not ok:
        raise ApiError(500, msg)
    return 202, {"started": True, "message": msg}


def live_start(cfg, q: dict, body: dict) -> tuple[int, dict]:
    """Start live HR; {"minutes": N} optional (default WHOOP_LIVE_MINUTES). Already on → 200, started false."""
    minutes = _num(body, "minutes", w.LIVE_MINUTES, 1, LIVE_MAX_MINUTES)
    if (d := w.live_status()) is not None:
        return 200, {"started": False, "message": "Live HR is already on", "running": d}
    return 202, {"started": True, "message": w.start_live(minutes), "minutes": minutes}


def live_extend(cfg, q: dict, body: dict) -> tuple[int, dict]:
    if not w.extend_live():
        raise ApiError(409, "Live HR isn't on")
    return 200, {"extended": True, "minutes": 10}


def live_stop(cfg, q: dict, body: dict) -> tuple[int, dict]:
    """Stop live HR; not running → 200 with stopped false."""
    return 200, {"stopped": w.stop_live()}


# (method, path) → (handler, heavy). Heavy handlers read and decode whoop.db; servers run them in a worker
# process so the cheap polls stay fast.
ROUTES = {
    ("GET", "/healthz"): (health, False),
    ("GET", "/v1/health"): (health, False),
    ("GET", "/v1/status"): (status, False),
    ("GET", "/v1/summary"): (summary, True),
    ("GET", "/v1/coach"): (summary, True),          # the original name of /v1/summary
    ("GET", "/v1/snapshot"): (snapshot, True),
    ("GET", "/v1/report"): (report, True),
    ("GET", "/v1/live"): (live, False),
    ("POST", "/v1/sync"): (sync_start, False),
    ("POST", "/v1/live/start"): (live_start, False),
    ("POST", "/v1/live/extend"): (live_extend, False),
    ("POST", "/v1/live/stop"): (live_stop, False),
}


def is_heavy(method: str, path: str) -> bool:
    return ROUTES.get((method, path), (None, False))[1]


def dispatch(cfg, method: str, path: str, query: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
    """(HTTP status, JSON document) for one request; the one entry for the server and client.py."""
    route = ROUTES.get((method, path))
    if route is None:
        if any(p == path for _, p in ROUTES):
            return 405, {"error": f"{method} not allowed on {path}"}
        return 404, {"error": f"no endpoint {path} (see API.md)"}
    try:
        return route[0](cfg, query or {}, body or {})
    except ApiError as e:
        return e.code, {"error": e.message}
    except Exception as e:                    # noqa: BLE001 - one bad request must not kill the server
        return 500, {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------------------- server
def make_server(cfg, host: str = HOST, port_: int = DEFAULT_PORT, pool=None, log=None):
    """A ThreadingHTTPServer on host:port_ (127.0.0.1 / localhost only) serving dispatch(). pool (a one-
    worker ProcessPoolExecutor) runs the heavy handlers; None runs them in the request thread."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qsl, urlsplit

    if host not in LOCAL_HOSTS:
        raise SystemExit(f"whoop-poc serve binds 127.0.0.1 only; refusing {host!r}")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, code: int, doc: dict) -> None:
            body = json.dumps(doc, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, method: str) -> None:
            t0 = time.monotonic()
            u = urlsplit(self.path)
            code, doc = self._route(method, u.path, dict(parse_qsl(u.query)))
            self._reply(code, doc)
            if log and (method != "GET" or code >= 400 or is_heavy(method, u.path)):   # not the 1 Hz polls
                log(f"{method} {u.path} {code} {1000 * (time.monotonic() - t0):.0f}ms")

        def _route(self, method: str, path: str, query: dict) -> tuple[int, dict]:
            host_hdr = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host_hdr not in LOCAL_HOSTS:
                return 403, {"error": "Host must be 127.0.0.1 or localhost"}
            body: dict = {}
            if method == "POST":
                if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                    return 415, {"error": "POST needs Content-Type: application/json"}
                n = int(self.headers.get("Content-Length") or 0)
                if n > 4096:
                    return 400, {"error": "body too large"}
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                except ValueError:
                    return 400, {"error": "body must be JSON"}
                if not isinstance(body, dict):
                    return 400, {"error": "body must be a JSON object"}
            if pool is not None and is_heavy(method, path):
                try:
                    return pool.submit(dispatch, cfg, method, path, query, body).result()
                except Exception as e:        # noqa: BLE001 - a broken worker is a 500, not a dead server
                    return 500, {"error": f"worker: {type(e).__name__}"}
            return dispatch(cfg, method, path, query, body)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def log_message(self, *_):
            pass

    srv = ThreadingHTTPServer((HOST if host == "localhost" else host, port_), Handler)
    srv.daemon_threads = True
    return srv


def serve(args) -> None:
    """`whoop-poc serve`: run until killed."""
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor

    host = getattr(args, "host", None) or HOST
    p = port(args)
    if host not in LOCAL_HOSTS:          # before the worker starts
        raise SystemExit(f"whoop-poc serve binds 127.0.0.1 only; refusing {host!r}")
    pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
    try:
        srv = make_server(args, host, p, pool, log=lambda m: print(m, file=sys.stderr, flush=True))
    except OSError as e:
        pool.shutdown(wait=False)
        raise SystemExit(f"can't listen on {HOST}:{p}: {e.strerror}") from None
    print(f"whoop-poc API on http://{HOST}:{p}/v1/ (see API.md) since "
          f"{datetime.now().strftime('%H:%M:%S')}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        pool.shutdown(wait=False, cancel_futures=True)

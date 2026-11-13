"""Coach webhook: POST each newly detected likely workout after a sync.

  whoop-poc webhook          post likely workouts not sent before (whoop-sync.service runs this after the
                             coach.json snapshot)
  whoop-poc webhook --test   post one sample payload marked "test": true; the state file is not touched

WHOOP_COACH_WEBHOOK_URL is the endpoint and WHOOP_COACH_WEBHOOK_AUTH the full Authorization header value,
both from ~/.config/whoop-poc/env (or the environment). With either unset it does nothing and exits 0.
Output names only workout ids and HTTP status codes, never the URL or the header value.

The state file (STATE_FILE in w.STATE_DIR) holds the ids already handled. The first run only records the
workouts detected then ("seeded"), so nothing old is posted. An id is recorded as sent only after a 2xx
response; a failed POST is retried on the next run.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import coach
import whoop_poc as w

STATE_SCHEMA = "whoop-poc/webhook-sent@1"
STATE_FILE = "webhook-sent.json"
TIMEOUT_S = 15
KEEP_DAYS = coach.ARCHIVE_DAYS      # ids older than this are dropped from the state file
PAYLOAD_KEYS = ("id", "start_unix", "end_unix", "minutes", "avg_hr", "max_hr", "needs_confirm", "zone_min")


def config() -> tuple[str, str] | None:
    """(url, authorization) or None when either WHOOP_COACH_WEBHOOK_URL or WHOOP_COACH_WEBHOOK_AUTH is unset."""
    url = os.environ.get("WHOOP_COACH_WEBHOOK_URL", "").strip()
    auth = os.environ.get("WHOOP_COACH_WEBHOOK_AUTH", "").strip()
    return (url, auth) if url and auth else None


def url_problem(url: str) -> str | None:
    """Why the URL can't carry the Authorization header (plain http other than loopback), else None."""
    u = urlsplit(url)
    if u.scheme == "https" and u.hostname:
        return None
    if u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"):
        return None
    return "WHOOP_COACH_WEBHOOK_URL must be an https URL (or http to localhost)"


def state_path() -> Path:
    return w.STATE_DIR / STATE_FILE


def load_state(path: Path) -> dict | None:
    """{schema, seeded_unix, seeded: [id], sent: {id: unix}}, or None before the first run."""
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("sent"), dict) or not isinstance(d.get("seeded"), list):
        return None
    return d


def prune(state: dict, today: date) -> None:
    """Drop ids whose day is more than KEEP_DAYS ago: they are out of every detection window."""
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    state["seeded"] = [i for i in state["seeded"] if i[:10] >= cutoff]
    state["sent"] = {i: t for i, t in state["sent"].items() if i[:10] >= cutoff}


def payload(x: dict) -> dict:
    """A workout record (coach.Window.workouts) as the webhook body. minutes = confirmed, else detected."""
    doc = {**{k: x[k] for k in PAYLOAD_KEYS if k in x}, "minutes": x["duration_min"]}
    if x.get("kind"):
        doc["kind"] = x["kind"]
    return doc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kw):    # a 3xx is a failure; the header never follows a redirect
        return None


def post(url: str, auth: str, doc: dict) -> tuple[int | None, bytes]:
    """POST doc as JSON. (HTTP status, first 4 KB of the body); status None when there was no response."""
    req = urllib.request.Request(url, data=json.dumps(doc).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": auth,
                                          "User-Agent": "whoop-poc"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read(4096)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError, ValueError):
        return None, b""


def ok(status: int | None) -> bool:
    return status is not None and 200 <= status < 300


def settled(x: dict, win: coach.Window) -> bool:
    """The data runs past the workout's end by the merge gap, so it can't grow any more."""
    return win.newest is not None and x["end_unix"] + coach.WORKOUT_MERGE_GAP_MIN * 60 <= win.newest


def send_new(win: coach.Window, url: str, auth: str, path: Path, send=post, log=print) -> int:
    """Post the likely workouts in win not yet seeded or sent. Returns 1 if any POST failed, else 0."""
    state = load_state(path)
    if state is None:
        ids = [x["id"] for x in win.workouts]
        w.write_json(path, {"schema": STATE_SCHEMA, "seeded_unix": win.now, "seeded": ids, "sent": {}})
        log(f"webhook: first run: recorded {len(ids)} existing workouts as seeded, posted none")
        return 0
    done = set(state["seeded"]) | set(state["sent"])
    code = 0
    for x in win.workouts:
        if x["id"] in done or not x["needs_confirm"] or not settled(x, win):
            continue
        status, _ = send(url, auth, payload(x))
        log(f"webhook: {x['id']} -> {status if status is not None else 'no response'}")
        if ok(status):
            state["sent"][x["id"]] = int(time.time())
        else:
            code = 1
    prune(state, coach.local_date(win.now))
    w.write_json(path, state)
    return code


def test_payload(now: int) -> dict:
    """A sample body for --test, marked test: true, with an id that can't match a real workout."""
    return {"test": True, "id": "test-" + coach.workout_id(now), "start_unix": now - 1800, "end_unix": now,
            "minutes": 30, "avg_hr": 150, "max_hr": 168, "needs_confirm": True, "zone_min": {"Z2": 10, "Z3": 20}}


def body_looks_ok(body: bytes) -> bool:
    """Empty, or JSON without an error / with ok|success true, or text without 'error'."""
    if not body.strip():
        return True
    try:
        d = json.loads(body)
    except ValueError:
        return b"error" not in body.lower()
    if isinstance(d, dict):
        if "ok" in d or "success" in d:
            return bool(d.get("ok", d.get("success")))
        return "error" not in d and "errors" not in d
    return True


def run(a, send=post, log=print) -> int:
    """`whoop-poc webhook [--test]` → exit code."""
    cfg = config()
    if cfg is None:
        return 0
    url, auth = cfg
    why = url_problem(url)
    if why:
        log(f"webhook: {why}; nothing sent")
        return 1
    if getattr(a, "test", False):
        status, body = send(url, auth, test_payload(int(time.time())))
        log(f"webhook: test -> {status if status is not None else 'no response'}"
            + (f", body looks like success: {'yes' if body_looks_ok(body) else 'no'}" if status is not None else ""))
        return 0 if ok(status) else 1
    if a.demo:
        log("webhook: not with --demo")
        return 2
    now = int(time.time())
    days = coach.window_days(coach.local_date(now), a.days)
    win = coach.load(a.db, days[0], now, address=a.address, heuristic=True)
    return send_new(win, url, auth, state_path(), send, log)

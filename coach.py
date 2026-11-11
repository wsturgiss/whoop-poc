"""Coach-facing output: a compact multi-day summary, small drill-down queries, and the daily archive.

Coach is an LLM assistant, so the summary carries per-day rows and detected workouts, never per-minute
arrays. Per-minute HR and sleep stage segments live in whoop.db and in the daily archive files
(daily/YYYY-MM-DD.json). Schemas and the read flow are documented in COACH.md.

  whoop-poc coach                      summary of the last WHOOP_COACH_DAYS days (whoop-poc/snapshot@2)
  whoop-poc coach since <unix>         the same, keeping only records that end after <unix>
  whoop-poc coach day YYYY-MM-DD       one day: summary row, sleep, workouts, gaps, hourly HR
  whoop-poc coach workout <start>      one workout with HR at 1-min resolution
"""
from __future__ import annotations

import json
import os
import statistics as st
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import whoop_poc as w     # importing it loads ~/.config/whoop-poc/env into os.environ

SUMMARY_SCHEMA = "whoop-poc/snapshot@2"
DAY_SCHEMA = "whoop-poc/day@1"
DAY_VIEW_SCHEMA = "whoop-poc/day-view@1"
WORKOUT_SCHEMA = "whoop-poc/workout@1"

TZ_NAME = os.environ.get("WHOOP_TZ", "America/New_York")    # local day boundaries for rows and the archive
TZ = ZoneInfo(TZ_NAME)
COACH_DAYS = int(os.environ.get("WHOOP_COACH_DAYS", 7))     # summary window, calendar days including today
ARCHIVE_DAYS = int(os.environ.get("WHOOP_ARCHIVE_DAYS", 30))  # daily/ files older than this are deleted
WORKOUT_ZONE = w.ZONES[-1]      # Z1 (50% of HR reserve): minutes at or above its low bound are "elevated"
WORKOUT_MIN = 10                # an elevated run must span at least this long to be a workout
WORKOUT_MERGE_GAP_MIN = 3       # elevated runs separated by less than this are one workout
GAP_MIN = 30                    # stretches without worn data at least this long are reported as gaps
MIN_REST_WORN_MIN = 120         # a day row reports rest_hr only with at least this much wear
SLEEP_LEAD_S = 12 * 3600        # load this much before the first day so its night is detected whole


# ------------------------------------------------------------------------------------------- time
def day_bounds(d: date) -> tuple[int, int]:
    """[start, end) of a local calendar day in unix seconds (23 or 25 h on DST change days)."""
    a = datetime(d.year, d.month, d.day, tzinfo=TZ)
    b = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
    return int(a.timestamp()), int(b.timestamp())


def local_date(t: int) -> date:
    return datetime.fromtimestamp(t, TZ).date()


def hm(t: int) -> str:
    return datetime.fromtimestamp(t, TZ).strftime("%H:%M")


def span_local(a: int, b: int) -> str:
    """'23:59-08:16': a readable local label next to the unix fields."""
    return f"{hm(a)}-{hm(b)}"


def window_days(today: date, n: int) -> list[date]:
    return [today - timedelta(days=i) for i in range(n - 1, -1, -1)]


# ---------------------------------------------------------------------------------------- derived
def zone_min(mins: list[w.Minute]) -> dict[str, int]:
    """Worn minutes per zone Z1..Z5 (a minute's zone is its mean HR's); zero zones are left out."""
    out = {}
    for z in w.ZONES[::-1]:
        n = sum(1 for m in mins if w.hr_zone(m.hr)[0] == z[0])
        if n:
            out[z[0]] = n
    return out


def detect_workouts(mins: list[w.Minute]) -> list[dict]:
    """Workouts: minutes whose mean HR is in WORKOUT_ZONE or above, merged across gaps shorter than
    WORKOUT_MERGE_GAP_MIN (cooler or missing minutes), kept if the run spans at least WORKOUT_MIN."""
    thr = w.zone_low(WORKOUT_ZONE)
    runs: list[list[int]] = []
    for m in mins:
        if m.hr < thr:
            continue
        if runs and m.t - runs[-1][1] < WORKOUT_MERGE_GAP_MIN * 60:
            runs[-1][1] = m.t + 60
        else:
            runs.append([m.t, m.t + 60])
    out = []
    for a, b in runs:
        if b - a < WORKOUT_MIN * 60:
            continue
        inside = [m for m in mins if a <= m.t < b]
        hrs = [m.hr for m in inside]
        out.append({"start_unix": a, "end_unix": b, "local": datetime.fromtimestamp(a, TZ).strftime("%m-%d ") + span_local(a, b),
                    "duration_min": (b - a) // 60, "avg_hr": round(st.mean(hrs)), "max_hr": round(max(hrs)),
                    "zone_min": zone_min(inside)})
    return out


def detect_gaps(mins: list[w.Minute]) -> list[dict]:
    """Stretches of at least GAP_MIN with no worn minute, between the first and newest worn minute.
    (Time before the first or after the newest sample is not a gap: see window / freshness.)"""
    out = []
    for p, n in zip(mins, mins[1:]):
        a, b = p.t + 60, n.t
        if b - a >= GAP_MIN * 60:
            out.append({"start_unix": a, "end_unix": b, "local": span_local(a, b), "min": (b - a) // 60})
    return out


def sleep_brief(s: w.SleepWindow) -> dict:
    """In bed = start..end (the `local` span); asleep_min excludes wake."""
    out = {"start_unix": s.start, "end_unix": s.end, "local": span_local(s.start, s.end), "asleep_min": s.asleep_min,
           "eff_pct": round(100 * s.asleep_min / s.span_min) if s.span_min else None}
    hrs = s.hrs
    out["resting_hr" if s.resting_hr is not None else "avg_hr"] = (
        s.resting_hr if s.resting_hr is not None else (round(st.mean(hrs)) if hrs else None))
    if s.stages is not None:
        out["stage_min"] = s.stage_minutes()
    return out


class Window:
    """Everything derived once from one Report: worn minutes, workouts, gaps, sleep by wake day."""

    def __init__(self, r: w.Report, now: int):
        self.r, self.now = r, now
        self.mins = r.hr_minutes
        self.newest = r.samples[-1].unix if r.samples else None
        self.drained_to = r.meta.get("newest_unix") or self.newest    # newest frame in the whole DB
        self.workouts = detect_workouts(self.mins)
        self.gaps = detect_gaps(self.mins)

    def final(self, d: date) -> bool:
        """The day is over and the strap has been drained past its end, so its data won't change."""
        e = day_bounds(d)[1]
        return e <= self.now and self.drained_to is not None and self.drained_to >= e

    def day_mins(self, d: date) -> list[w.Minute]:
        a, b = day_bounds(d)
        return [m for m in self.mins if a <= m.t < b]

    def day_sleep(self, d: date) -> list[w.SleepWindow]:
        """Sleep sessions credited to the day they end on (last night belongs to today)."""
        a, b = day_bounds(d)
        return [s for s in self.r.sleep if a <= s.end <= b]

    def day_workouts(self, d: date) -> list[dict]:
        a, b = day_bounds(d)
        return [x for x in self.workouts if a <= x["start_unix"] < b]

    def day_gaps(self, d: date) -> list[dict]:
        a, b = day_bounds(d)
        return [g for g in self.gaps if g["end_unix"] > a and g["start_unix"] < b]

    def day_row(self, d: date) -> dict:
        a, b = day_bounds(d)
        mins = self.day_mins(d)
        row: dict = {"date": d.isoformat(), "start_unix": a, "worn_min": len(mins)}
        if not self.final(d):
            row["open"] = True
        # minutes with no worn data between the window's first and newest worn minute (strap off)
        lo = max(a, self.mins[0].t) if self.mins else b
        covered = max(0, min(b, self.now, (self.newest or a) + 60) - lo) // 60
        if covered - len(mins) > 0:
            row["unworn_min"] = covered - len(mins)
        s = w.minute_stats(mins)
        if s:
            if len(mins) >= MIN_REST_WORN_MIN:
                row["rest_hr"] = round(s["rest"])
            row.update(avg_hr=round(s["avg"]), max_hr=round(s["max"]))
            z = zone_min(mins)
            if z:
                row["zone_min"] = z
        sleep = self.day_sleep(d)
        if sleep:
            row["sleep"] = sleep_brief(max(sleep, key=lambda s: s.asleep_min))
            if len(sleep) > 1:
                row["sleep_sessions"] = len(sleep)
        return row


WORKOUT_COLS = ["start_unix", "end_unix", "local", "duration_min", "avg_hr", "max_hr", "zone_min"]


def table(records: list[dict], cols: list[str]) -> dict:
    """{"cols": [...], "rows": [[...], ...]}: repeated records without repeating their keys."""
    return {"cols": cols, "rows": [[x[c] for c in cols] for x in records]}


def trends(rows: list[dict]) -> dict:
    """Latest value of a few metrics against the mean of the earlier days in the window."""
    def active(r):    # minutes in Z1+; only complete days, a day still open is partial
        return None if r.get("open") or not r["worn_min"] else sum(r.get("zone_min", {}).values())
    metrics = {
        "rest_hr": lambda r: r.get("rest_hr"),
        "sleep_asleep_min": lambda r: r.get("sleep", {}).get("asleep_min"),
        "sleep_eff_pct": lambda r: r.get("sleep", {}).get("eff_pct"),
        "active_min": active,
    }
    out = {}
    for k, get in metrics.items():
        vals = [(r["date"], get(r)) for r in rows if get(r) is not None]   # latest = the last day that has it
        if not vals:
            continue
        latest = vals[-1][1]
        base = [v for _, v in vals[:-1]]
        mean = round(st.mean(base), 1) if base else None
        out[k] = {"latest": latest, "baseline": mean,
                  "delta": None if mean is None else round(latest - mean, 1), "n": len(base)}
    return out


# ------------------------------------------------------------------------------------- documents
def load(db: Path, first: date, now: int, address=None, demo=False, heuristic=False) -> Window:
    since = day_bounds(first)[0] - SLEEP_LEAD_S
    r = w.build_report(db, (now - since) / 3600, address, now=now, demo=demo, heuristic=heuristic)
    return Window(r, now)


def freshness(win: Window) -> dict:
    demo = win.r.meta.get("address") == "-"
    sync = None if demo else w.last_sync_attempt()
    bat = None if demo else w.last_battery()
    return {"data_age_min": None if win.newest is None else round((win.now - win.newest) / 60),
            "last_sync": None if not sync else {k: sync.get(k) for k in ("outcome", "finished_unix")},
            "battery_pct": None if not bat else bat["pct"]}


def summary(win: Window, days: list[date], since: int | None = None) -> dict:
    """whoop-poc/snapshot@2. With `since`, only records ending after it (days still open count)."""
    rows = [win.day_row(d) for d in days]
    first = day_bounds(days[0])[0]
    workouts = [x for x in win.workouts if x["start_unix"] >= first]
    gaps = [g for g in win.gaps if g["end_unix"] > first]
    trend = trends(rows)
    if since is not None:
        rows = [r for r in rows if day_bounds(date.fromisoformat(r["date"]))[1] > since]
        workouts = [x for x in workouts if x["end_unix"] > since]
        gaps = [g for g in gaps if g["end_unix"] > since]
    latest = win.r.hr_now
    doc = {
        "schema": SUMMARY_SCHEMA,
        "demo": win.r.meta.get("address") == "-",
        "generated_unix": win.now, "tz": TZ_NAME,
        "window": {"since_unix": first, "days": len(days),
                   "data_since_unix": next((m.t for m in win.mins if m.t >= first), None)},
        "cursor_unix": win.newest,           # newest sample; pass it to `coach since` next time
        "freshness": freshness(win),
        "latest_hr": None if latest is None else {"unix": latest.unix, "bpm": latest.hr},
        "zones": {"max_hr": w.MAX_HR, "rest_hr": w.REST_HR, **{z[0]: w.zone_low(z) for z in w.ZONES[::-1]}},
        "sleep_source": win.r.meta.get("sleep_source"),
        "days": rows, "workouts": table(workouts, WORKOUT_COLS), "gaps": gaps, "trends": trend,
    }
    if since is not None:
        doc["since_unix"] = since
    return doc


def day_doc(win: Window, d: date) -> dict:
    """whoop-poc/day@1: the archive file for one local day, per-minute HR and stage segments included."""
    a, b = day_bounds(d)
    labels = [{"start_unix": x, "end_unix": y, "activity": act}
              for x, y, act in win.r.meta.get("labels", []) if y > a and x < b]
    return {
        "schema": DAY_SCHEMA, "date": d.isoformat(), "tz": TZ_NAME, "start_unix": a, "end_unix": b,
        "final": win.final(d), "generated_unix": win.now, "newest_sample_unix": win.newest,
        "summary": win.day_row(d),
        "workouts": win.day_workouts(d), "gaps": win.day_gaps(d),
        "sleep": {"source": win.r.meta.get("sleep_source"), "sessions": [w.sleep_session(s) for s in win.day_sleep(d)]},
        "labels": labels,
        "hr_per_minute": [[m.t, round(m.hr, 1)] for m in win.day_mins(d)],
    }


def day_view(win: Window, d: date) -> dict:
    """whoop-poc/day-view@1: one day small enough for Coach to read: hourly HR instead of per-minute."""
    hours: dict[int, list[w.Minute]] = {}
    for m in win.day_mins(d):
        hours.setdefault(m.t // 3600 * 3600, []).append(m)
    return {
        "schema": DAY_VIEW_SCHEMA, "date": d.isoformat(), "tz": TZ_NAME, "final": win.final(d),
        "summary": win.day_row(d),
        "sleep": [sleep_brief(s) for s in win.day_sleep(d)],
        "workouts": win.day_workouts(d), "gaps": win.day_gaps(d),
        "hourly": {"cols": ["start_unix", "local", "worn_min", "avg_hr", "max_hr"],
                   "rows": [[t, hm(t), len(v), round(st.mean(m.hr for m in v)), round(max(m.hr for m in v))]
                            for t, v in sorted(hours.items())]},
        "archive": str(w.STATE_DIR / "daily" / f"{d.isoformat()}.json"),
    }


def workout_view(win: Window, t: int) -> dict | None:
    """whoop-poc/workout@1: the workout containing (or starting nearest, within 15 min) time t."""
    def dist(x):
        return 0 if x["start_unix"] <= t < x["end_unix"] else min(abs(x["start_unix"] - t), abs(x["end_unix"] - t))
    near = [x for x in win.workouts if dist(x) <= 15 * 60]
    if not near:
        return None
    x = min(near, key=dist)
    peak = max((s.hr for s in win.r.samples if x["start_unix"] <= s.unix < x["end_unix"]), default=None)
    return {"schema": WORKOUT_SCHEMA, "tz": TZ_NAME, **x, "peak_hr_1s": peak,
            "zones": {"max_hr": w.MAX_HR, "rest_hr": w.REST_HR},
            "hr_per_minute": [[m.t, round(m.hr)] for m in win.mins if x["start_unix"] <= m.t < x["end_unix"]]}


# --------------------------------------------------------------------------------------- archive
def _comparable(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in ("generated_unix", "newest_sample_unix")}


def write_archive(win: Window, days: list[date], out_dir: Path, keep_days: int = ARCHIVE_DAYS,
                  today: date | None = None) -> list[Path]:
    """Write daily/YYYY-MM-DD.json for each day: rewritten while the day is open and its content changes,
    left alone once a file marked final exists. Deletes files older than keep_days. Returns written paths."""
    written = []
    for d in days:
        p = out_dir / f"{d.isoformat()}.json"
        try:
            old = json.loads(p.read_text())
        except (OSError, ValueError):
            old = None
        if old and old.get("final"):
            continue
        doc = day_doc(win, d)
        if not doc["summary"]["worn_min"] and not doc["sleep"]["sessions"] and not old:
            continue      # nothing recorded that day (before the strap's first data)
        if old and _comparable(old) == _comparable(doc):
            continue
        w.write_json(p, doc, compact=True)
        written.append(p)
    cutoff = (today or local_date(win.now)) - timedelta(days=keep_days)
    for p in out_dir.glob("*.json"):
        try:
            if date.fromisoformat(p.stem) < cutoff:
                p.unlink()
        except ValueError:
            pass
    return written


# ------------------------------------------------------------------------------------------- CLI
def parse_time(s: str, now: int) -> int:
    """unix seconds, 'YYYY-MM-DDTHH:MM[:SS]' or 'HH:MM' (today), local times in TZ."""
    if s.isdigit():
        return int(s)
    if len(s) <= 5 and ":" in s:
        h, m = map(int, s.split(":"))
        return int(datetime.combine(local_date(now), datetime.min.time(), tzinfo=TZ).replace(hour=h, minute=m).timestamp())
    dt = datetime.fromisoformat(s)
    return int((dt if dt.tzinfo else dt.replace(tzinfo=TZ)).timestamp())


def run(a, rest: list[str]) -> tuple[dict, int]:
    """`whoop-poc coach [since <unix> | day <date> | workout <start>]` → (document, exit code)."""
    now = w.DEMO_END if a.demo else int(time.time())
    today = local_date(now)
    kw = dict(address=a.address, demo=a.demo, heuristic=a.heuristic)
    sub = rest[0] if rest else "summary"
    if sub == "summary" and len(rest) <= 1:
        days = window_days(today, a.days)
        return summary(load(a.db, days[0], now, **kw), days), 0
    if sub == "since" and len(rest) == 2:
        since = parse_time(rest[1], now)       # load at least the summary window so trends keep their baseline
        first = max(min(local_date(since), today - timedelta(days=a.days - 1)), today - timedelta(days=ARCHIVE_DAYS - 1))
        days = [first + timedelta(days=i) for i in range((today - first).days + 1)]
        return summary(load(a.db, days[0], now, **kw), days, since=since), 0
    if sub == "day" and len(rest) == 2:
        d = today if rest[1] == "today" else today - timedelta(days=1) if rest[1] == "yesterday" else date.fromisoformat(rest[1])
        return day_view(load(a.db, d, min(now, day_bounds(d)[1]), **kw), d), 0
    if sub == "workout" and len(rest) == 2:
        t = parse_time(rest[1], now)
        d = local_date(t)
        win = load(a.db, d, min(now, day_bounds(d)[1] + 6 * 3600), **kw)
        doc = workout_view(win, t)
        if doc is None:
            return {"error": f"no workout within 15 min of {rest[1]}", "workouts": win.day_workouts(d)}, 1
        return doc, 0
    return {"error": "usage: whoop-poc coach [since <unix> | day <YYYY-MM-DD|today|yesterday> | workout <unix|HH:MM|ISO>]"}, 2


def write_snapshot(a, out: Path, archive: bool = True) -> dict:
    """What whoop-sync.service runs after each attempt (`whoop-poc json --out coach.json`): the summary
    to `out`, and the window's days to out's sibling daily/ directory."""
    now = w.DEMO_END if a.demo else int(time.time())
    days = window_days(local_date(now), a.days)
    win = load(a.db, days[0], now, address=a.address, demo=a.demo, heuristic=a.heuristic)
    doc = summary(win, days)
    w.write_json(out, doc, compact=True)
    if archive and not doc["demo"]:
        write_archive(win, days, out.parent / "daily")
    return doc

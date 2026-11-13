"""Coach-facing output: a compact multi-day summary, small drill-down queries, and the daily archive.

Coach is an LLM assistant, so the summary carries per-day rows and detected workouts, never per-minute
arrays. Per-minute HR and sleep stage segments live in whoop.db and in the daily archive files
(daily/YYYY-MM-DD.json). Schemas and the read flow are documented in COACH.md.

  whoop-poc coach                      summary of the last WHOOP_COACH_DAYS days (whoop-poc/snapshot@3)
  whoop-poc coach since <unix>         the same, keeping only records that end after <unix>
  whoop-poc coach day YYYY-MM-DD       one day: summary row, sleep, workouts, gaps, hourly HR
  whoop-poc coach workout <id>         one workout's computed detail (peaks, recovery, drift, intervals, …)
  whoop-poc coach confirm <id> --minutes N [--start HH:MM]
                                       record the wearer's confirmed duration (--clear removes it)
"""
from __future__ import annotations

import json
import os
import socket
import statistics as st
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import whoop_poc as w     # importing it loads ~/.config/whoop-poc/env into os.environ
import workout_detail as wd

SUMMARY_SCHEMA = "whoop-poc/snapshot@3"
DAY_SCHEMA = "whoop-poc/day@2"
DAY_VIEW_SCHEMA = "whoop-poc/day-view@2"
WORKOUT_SCHEMA = "whoop-poc/workout@2"
CONFIRM_SCHEMA = "whoop-poc/workout-confirm@1"
CONFIRM_FILE = "workout-confirm.json"     # in w.STATE_DIR, next to coach.json; never in whoop.db

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
CONFIRM_MATCH_S = 10 * 60       # a confirmation applies to the detected workout starting nearest, within this
NIGHT_MIN_SPAN_MIN = 180        # a sleep session shorter than this (in bed) is a nap, not a night
NIGHT_HOURS = (20, 11)          # local evening-to-morning window [20:00, 11:00) ...
NIGHT_MIN_FRAC = 0.5            # ... that at least this much of a night's in-bed span must fall in
MIN_BASELINE_N = 3              # trends report a baseline and delta only with this many earlier days with data
STALE_MIN = 60                  # data older than this, with last_sync not_advertising, gets freshness.hint


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


def workout_id(t: int) -> str:
    """'2026-11-11-0824': a workout's readable ID, its detected start in local time."""
    return datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d-%H%M")


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
        out.append({"id": workout_id(a), "start_unix": a, "end_unix": b, "local": datetime.fromtimestamp(a, TZ).strftime("%m-%d ") + span_local(a, b),
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


def is_night(s: w.SleepWindow) -> bool:
    """A night's sleep: in bed at least NIGHT_MIN_SPAN_MIN, mostly inside the local NIGHT_HOURS window.
    Shorter or daytime sessions are naps: kept in drill-downs, left out of the row's sleep and trends."""
    if s.span_min < NIGHT_MIN_SPAN_MIN:
        return False
    a, b = NIGHT_HOURS
    inside = sum(1 for t in range(s.start, s.end, 60)
                 if (h := datetime.fromtimestamp(t, TZ).hour) >= a or h < b)
    return inside >= NIGHT_MIN_FRAC * s.span_min


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


# ---------------------------------------------------------------------------------- confirmations
# A confirmation is the wearer's own duration for one detected workout:
#   {"start_unix": detected start when confirmed, "minutes": N, "actual_start_unix": unix | absent, "set_unix": …}
# It is matched to workouts by start_unix within CONFIRM_MATCH_S, so it still applies after re-detection.
CONFIRM_KEYS = ("detected_min", "confirmed", "needs_confirm", "confirmed_start_unix")


def confirm_path() -> Path:
    return w.STATE_DIR / CONFIRM_FILE


def load_confirmations(path: Path | None = None) -> list[dict]:
    try:
        return list(json.loads((path or confirm_path()).read_text()).get("workouts", []))
    except (OSError, ValueError, AttributeError):
        return []


def save_confirmations(items: list[dict], path: Path | None = None) -> None:
    w.write_json(path or confirm_path(), {"schema": CONFIRM_SCHEMA,
                                          "workouts": sorted(items, key=lambda c: c["start_unix"])})


def match_confirmations(workouts: list[dict], confs: list[dict]) -> dict[int, dict]:
    """{index into workouts: confirmation}. Each confirmation goes to the workout whose start is nearest
    (within CONFIRM_MATCH_S); a workout claimed by two keeps the nearer one."""
    out: dict[int, tuple[int, dict]] = {}
    for c in confs:
        near = [(abs(x["start_unix"] - c["start_unix"]), i) for i, x in enumerate(workouts)
                if abs(x["start_unix"] - c["start_unix"]) <= CONFIRM_MATCH_S]
        if not near:
            continue
        d, i = min(near)
        if i not in out or d < out[i][0]:
            out[i] = (d, c)
    return {i: c for i, (_, c) in out.items()}


def apply_confirmations(workouts: list[dict], confs: list[dict]) -> list[dict]:
    """Workouts with duration_min = the confirmed minutes where confirmed, else the detected minutes.
    detected_min keeps the HR-elevated window's length; needs_confirm marks the ones to ask about.
    Accepts workouts that already went through this (archive files): it starts from detected_min."""
    matched = match_confirmations(workouts, confs)
    out = []
    for i, x in enumerate(workouts):
        y = {k: v for k, v in x.items() if k not in CONFIRM_KEYS}
        detected = x.get("detected_min", x["duration_min"])
        c = matched.get(i)
        y.update(duration_min=c["minutes"] if c else detected, detected_min=detected,
                 confirmed=c is not None, needs_confirm=c is None,
                 confirmed_start_unix=c.get("actual_start_unix") if c else None)
        out.append(y)
    return out


class Window:
    """Everything derived once from one Report: worn minutes, workouts, gaps, sleep by wake day."""

    def __init__(self, r: w.Report, now: int, confirmations: list[dict] | None = None):
        self.r, self.now = r, now
        self.mins = r.hr_minutes
        self.newest = r.samples[-1].unix if r.samples else None
        self.drained_to = r.meta.get("newest_unix") or self.newest    # newest frame in the whole DB
        demo = r.meta.get("address") == "-"
        self.confirmations = [] if demo else load_confirmations() if confirmations is None else confirmations
        self.workouts = apply_confirmations(detect_workouts(self.mins), self.confirmations)
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
        """One day's summary row. A day with no worn minute at all is {date, start_unix, no_data: true}
        (plus `open`): it carries no worn_min, because 0 would claim the strap was off."""
        a, b = day_bounds(d)
        mins = self.day_mins(d)
        row: dict = {"date": d.isoformat(), "start_unix": a}
        if not self.final(d):
            row["open"] = True
        if not mins:
            row["no_data"] = True
            return row
        row["worn_min"] = len(mins)
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
        row["active_min"] = self.active_min(d)
        sleep = self.day_sleep(d)
        nights = [s for s in sleep if is_night(s)]
        if nights:
            row["sleep"] = sleep_brief(max(nights, key=lambda s: s.asleep_min))
            if len(nights) > 1:
                row["sleep_sessions"] = len(nights)
        naps = sum(s.asleep_min for s in sleep if not is_night(s))
        if naps:
            row["nap_min"] = naps
        return row

    def active_min(self, d: date) -> int | None:
        """None for a day with no worn minute. Otherwise the duration_min (confirmed, else detected) of the
        workouts starting this day, plus minutes at or above Z1 outside any detected workout window."""
        mins = self.day_mins(d)
        if not mins:
            return None
        wk = self.day_workouts(d)
        thr = w.zone_low(WORKOUT_ZONE)
        loose = sum(1 for m in mins if m.hr >= thr
                    and not any(x["start_unix"] <= m.t < x["end_unix"] for x in wk))
        return sum(x["duration_min"] for x in wk) + loose


# The summary's workout table. Objects (day files, day view, workout view) also carry `confirmed` and
# `confirmed_start_unix`; in the table a confirmed row is one with needs_confirm false.
WORKOUT_COLS = ["start_unix", "end_unix", "local", "duration_min", "avg_hr", "max_hr", "zone_min",
                "detected_min", "needs_confirm", "id"]


def table(records: list[dict], cols: list[str]) -> dict:
    """{"cols": [...], "rows": [[...], ...]}: repeated records without repeating their keys."""
    return {"cols": cols, "rows": [[x[c] for c in cols] for x in records]}


def trends(rows: list[dict]) -> dict:
    """Latest value of a few metrics against the mean of the earlier days that have it. n counts those
    earlier days; baseline and delta stay null (with `why`) until n >= MIN_BASELINE_N."""
    def active(r):    # an open day is partial: it can be the latest, never part of the baseline
        return None if r.get("open") and r is not rows[-1] else r.get("active_min")
    metrics = {
        "rest_hr": (lambda r: r.get("rest_hr"), "days"),
        "sleep_asleep_min": (lambda r: r.get("sleep", {}).get("asleep_min"), "nights"),
        "sleep_eff_pct": (lambda r: r.get("sleep", {}).get("eff_pct"), "nights"),
        "active_min": (active, "days"),
    }
    out = {}
    for k, (get, unit) in metrics.items():
        vals = [v for v in map(get, rows) if v is not None]     # latest = the last day that has it
        if not vals:
            continue
        latest, base = vals[-1], vals[:-1]
        t = {"latest": latest, "baseline": None, "delta": None, "n": len(base)}
        if len(base) >= MIN_BASELINE_N:
            mean = round(st.mean(base), 1)
            t.update(baseline=mean, delta=round(latest - mean, 1))
        else:
            t["why"] = f"baseline needs {MIN_BASELINE_N} earlier {unit} with data, has {len(base)}"
        out[k] = t
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
    age = None if win.newest is None else round((win.now - win.newest) / 60)
    out = {"data_age_min": age,
           "last_sync": None if not sync else {k: sync.get(k) for k in ("outcome", "finished_unix")},
           "battery_pct": None if not bat else bat["pct"]}
    if sync and sync.get("outcome") == "not_advertising" and (age is None or age > STALE_MIN):
        since = "no data yet" if age is None else f"no new data for {age // 60} h {age % 60} min"
        out["hint"] = f"strap out of range of {socket.gethostname()} (not advertising); {since}"
    return out


def zones() -> dict:
    return {"max_hr": w.MAX_HR, "rest_hr": w.REST_HR, **{z[0]: w.zone_low(z) for z in w.ZONES[::-1]}}


def summary(win: Window, days: list[date], since: int | None = None) -> dict:
    """whoop-poc/snapshot@3. With `since`, only records ending after it (days still open count), plus
    workouts confirmed after it."""
    rows = [win.day_row(d) for d in days]
    first = day_bounds(days[0])[0]
    workouts = [x for x in win.workouts if x["start_unix"] >= first]
    gaps = [g for g in win.gaps if g["end_unix"] > first]
    trend = trends(rows)
    if since is not None:
        rows = [r for r in rows if day_bounds(date.fromisoformat(r["date"]))[1] > since]
        recent = {workouts[i]["start_unix"] for i, c in match_confirmations(workouts, win.confirmations).items()
                  if c.get("set_unix", 0) > since}
        workouts = [x for x in workouts if x["end_unix"] > since or x["start_unix"] in recent]
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
        "zones": zones(),
        "sleep_source": win.r.meta.get("sleep_source"),
        "days": rows, "workouts": table(workouts, WORKOUT_COLS), "gaps": gaps, "trends": trend,
    }
    if since is not None:
        doc["since_unix"] = since
    return doc


def day_doc(win: Window, d: date) -> dict:
    """whoop-poc/day@2: the archive file for one local day, per-minute HR and stage segments included."""
    a, b = day_bounds(d)
    labels = [{"start_unix": x, "end_unix": y, "activity": act}
              for x, y, act in win.r.meta.get("labels", []) if y > a and x < b]
    return {
        "schema": DAY_SCHEMA, "date": d.isoformat(), "tz": TZ_NAME, "start_unix": a, "end_unix": b,
        "final": win.final(d), "generated_unix": win.now, "newest_sample_unix": win.newest,
        "summary": win.day_row(d),
        "workouts": win.day_workouts(d), "gaps": win.day_gaps(d),
        "sleep": {"source": win.r.meta.get("sleep_source"), "sessions": [{**w.sleep_session(s), "night": is_night(s)}
                                                                for s in win.day_sleep(d)]},
        "labels": labels,
        "hr_per_minute": [[m.t, round(m.hr, 1)] for m in win.day_mins(d)],
    }


def day_view(win: Window, d: date) -> dict:
    """whoop-poc/day-view@2: one day small enough for Coach to read: hourly HR instead of per-minute."""
    hours: dict[int, list[w.Minute]] = {}
    for m in win.day_mins(d):
        hours.setdefault(m.t // 3600 * 3600, []).append(m)
    return {
        "schema": DAY_VIEW_SCHEMA, "date": d.isoformat(), "tz": TZ_NAME, "final": win.final(d),
        "summary": win.day_row(d),
        "sleep": [{**sleep_brief(s), "night": is_night(s)} for s in win.day_sleep(d)],
        "workouts": win.day_workouts(d), "gaps": win.day_gaps(d),
        "hourly": {"cols": ["start_unix", "local", "worn_min", "avg_hr", "max_hr"],
                   "rows": [[t, hm(t), len(v), round(st.mean(m.hr for m in v)), round(max(m.hr for m in v))]
                            for t, v in sorted(hours.items())]},
        "archive": str(w.STATE_DIR / "daily" / f"{d.isoformat()}.json"),
    }


def find_workout(win: Window, t: int) -> dict | None:
    """The workout containing time t, or else the one starting or ending nearest to it, within 15 min."""
    def dist(x):
        return 0 if x["start_unix"] <= t < x["end_unix"] else min(abs(x["start_unix"] - t), abs(x["end_unix"] - t))
    near = [x for x in win.workouts if dist(x) <= 15 * 60]
    return min(near, key=dist) if near else None


WORKOUT_KINDS = ("run", "lift")
SERIES = ("1m", "10s")


def workout_view(win: Window, t: int, kind: str | None = None, series: str | None = None,
                 db: Path | None = None) -> dict | None:
    """whoop-poc/workout@2: one workout's computed detail (definitions in COACH.md, "Workout detail").
    kind "lift" swaps the cardio fields for set detection. series "1m" or "10s" adds an HR array."""
    x = find_workout(win, t)
    if x is None:
        return None
    a, b = x["start_unix"], x["end_unix"]
    hr = wd.hr_seconds(win.r.samples, a - 60, b + 600)
    inside = {u: v for u, v in hr.items() if a <= u < b}
    doc = {"schema": WORKOUT_SCHEMA, "tz": TZ_NAME, **x, "kind": kind,
           "peak_hr_5s": wd.peak_5s(inside), "peak_hr_1s": max(inside.values(), default=None), "zones": zones()}
    if kind == "lift":
        doc["lifting"] = wd.lift_sets(win.r.samples, a, b)
    else:
        end = wd.effort_end(hr, a, b, x["avg_hr"])
        segs = wd.intervals(hr, a, end) if end else []
        imu = wd.load_imu(db, a, end or b) if db else []
        doc.update(z4_plus_min=wd.z4_plus_min(inside),
                   effort_end_unix=end,
                   effort_end_local=None if end is None else datetime.fromtimestamp(end, TZ).strftime("%H:%M:%S"),
                   recovery=wd.recovery(hr, end),
                   drift=wd.drift(hr, a, end, bool(segs)) if end else {"steady": False, "drift_bpm": None,
                                                                       "why": "no effort end found"},
                   intervals=[{**g, "local": datetime.fromtimestamp(g["start_unix"], TZ).strftime("%H:%M:%S")}
                              for g in segs],
                   cadence=wd.cadence(imu))
    if series == "1m":
        doc["hr_per_minute"] = [[m.t, round(m.hr)] for m in win.mins if a <= m.t < b]
    elif series == "10s":
        doc["hr_10s"] = [[u, round(v)] for u, v in wd.bins(hr, a, b + 180)]
    return doc


TUI_WORKOUT_KEYS = ("id", "start_unix", "end_unix", "duration_min", "detected_min", "confirmed",
                    "avg_hr", "max_hr", "zone_min", "recovery")


def workout_rows(win: Window) -> list[dict]:
    """The window's workouts, oldest first, with the workout@2 fields the TUI shows."""
    return [{k: doc[k] for k in TUI_WORKOUT_KEYS}
            for doc in (workout_view(win, x["start_unix"]) for x in win.workouts)]


def report_with_workouts(*args, **kw) -> w.Report:
    """w.build_report_slim plus meta["workouts"] = workout_rows, computed before the samples are dropped
    (recovery needs per-second HR). The TUI's report builder."""
    r = w.build_report(*args, **kw)
    r.meta["workouts"] = workout_rows(Window(r, r.until))
    return w.slim_report(r)


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
            if patch_day_file(p, win.confirmations, old):
                written.append(p)
            continue
        doc = day_doc(win, d)
        if doc["summary"].get("no_data") and not doc["sleep"]["sessions"] and not old:
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


def patch_day_file(p: Path, confs: list[dict], doc: dict | None = None) -> bool:
    """Re-apply confirmations to an archive file's workouts; rewrite it (keeping `final`) if they changed."""
    try:
        doc = doc if doc is not None else json.loads(p.read_text())
    except (OSError, ValueError):
        return False
    new = apply_confirmations(doc.get("workouts", []), confs)
    if new == doc.get("workouts", []):
        return False
    w.write_json(p, {**doc, "workouts": new}, compact=True)
    return True


def patch_written(confs: list[dict]) -> list[Path]:
    """After a confirm: re-apply confirmations to coach.json and every daily/ file, without reading the DB,
    so Coach sees the change before the next sync rewrites them. Returns the paths rewritten."""
    written = [p for p in sorted((w.STATE_DIR / "daily").glob("*.json")) if patch_day_file(p, confs)]
    p = w.STATE_DIR / "coach.json"
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        return written
    if doc.get("demo") or "workouts" not in doc:
        return written
    old = doc["workouts"]
    new = table(apply_confirmations([dict(zip(old["cols"], r)) for r in old["rows"]], confs), WORKOUT_COLS)
    if new != old:
        w.write_json(p, {**doc, "workouts": new}, compact=True)
        written.append(p)
    return written


def confirm(a, rest: list[str], now: int) -> tuple[dict, int]:
    """`coach confirm <start> --minutes N [--start HH:MM]` or `coach confirm <start> --clear`."""
    if a.demo:
        return {"error": "confirm writes the wearer's confirmations; not with --demo"}, 2
    minutes, clear = getattr(a, "minutes", None), getattr(a, "clear", False)
    if clear == (minutes is not None):
        return {"error": "usage: whoop-poc coach confirm <start> (--minutes N [--start HH:MM] | --clear)"}, 2
    if minutes is not None and not 1 <= minutes <= 24 * 60:
        return {"error": "--minutes must be 1..1440"}, 2
    t = parse_time(rest[1], now)
    d = local_date(t)
    win = load(a.db, d, min(now, day_bounds(d)[1] + 6 * 3600), address=a.address, heuristic=a.heuristic)
    x = find_workout(win, t)
    confs = load_confirmations()
    key = x["start_unix"] if x else t
    keep = [c for c in confs if abs(c["start_unix"] - key) > CONFIRM_MATCH_S]
    if clear:
        if len(keep) == len(confs):
            return {"error": f"no confirmation within {CONFIRM_MATCH_S // 60} min of {rest[1]}"}, 1
    else:
        if x is None:
            return {"error": f"no workout within 15 min of {rest[1]}", "workouts": win.day_workouts(d)}, 1
        c = {"start_unix": x["start_unix"], "minutes": minutes, "set_unix": now}
        if getattr(a, "start", None):
            c["actual_start_unix"] = parse_time(a.start, x["start_unix"])
        keep.append(c)
    save_confirmations(keep)
    written = patch_written(keep)
    workout = None
    if x:
        workout = apply_confirmations([x], keep)[0]
    return {"schema": CONFIRM_SCHEMA, "cleared" if clear else "recorded": True, "workout": workout,
            "file": str(confirm_path()), "rewritten": [p.name for p in written]}, 0


# ------------------------------------------------------------------------------------------- CLI
def parse_time(s: str, now: int) -> int:
    """unix seconds, a workout ID 'YYYY-MM-DD-HHMM', 'YYYY-MM-DDTHH:MM[:SS]' or 'HH:MM' (today), local times in TZ."""
    if s.isdigit():
        return int(s)
    if len(s) == 15 and s[10] == "-" and s[11:].isdigit():
        return int(datetime.strptime(s, "%Y-%m-%d-%H%M").replace(tzinfo=TZ).timestamp())
    if len(s) <= 5 and ":" in s:
        h, m = map(int, s.split(":"))
        return int(datetime.combine(local_date(now), datetime.min.time(), tzinfo=TZ).replace(hour=h, minute=m).timestamp())
    dt = datetime.fromisoformat(s)
    return int((dt if dt.tzinfo else dt.replace(tzinfo=TZ)).timestamp())


def run(a, rest: list[str]) -> tuple[dict, int]:
    """`whoop-poc coach [since <unix> | day <date> | workout <start> | confirm <start> …]` → (document, exit code)."""
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
        kind, series = getattr(a, "kind", None), getattr(a, "series", None)
        if kind not in (None, *WORKOUT_KINDS) or series not in (None, *SERIES):
            return {"error": f"--kind is one of {', '.join(WORKOUT_KINDS)}; --series one of {', '.join(SERIES)}"}, 2
        doc = workout_view(win, t, kind, series, db=None if a.demo else a.db)
        if doc is None:
            return {"error": f"no workout within 15 min of {rest[1]}", "workouts": win.day_workouts(d)}, 1
        return doc, 0
    if sub == "confirm" and len(rest) == 2:
        return confirm(a, rest, now)
    return {"error": "usage: whoop-poc coach [since <unix> | day <YYYY-MM-DD|today|yesterday> | workout <id|unix|HH:MM|ISO> [--kind run|lift] [--series 1m|10s]"
                     " | confirm <start> (--minutes N [--start HH:MM] | --clear)]"}, 2


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

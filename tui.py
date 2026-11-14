"""Textual TUI for whoop-poc: heart rate + approximate sleep from a local NOOP whoop.db."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

import api
import coach
import whoop_poc as core
from client import Client


class Tile(Static):
    def show(self, label: str, value: str, note: str = "") -> None:
        self.update(f"[dim]{label}[/]\n[b]{value}[/]\n[dim]{note}[/]")


SETTINGS_PATH = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "whoop-poc" / "tui.json"
WINDOW_STEPS = (24, 72, 168)            # hours the w key cycles through


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    """TUI settings saved by save_settings ({"theme": str, "hours": int}); {} if missing or invalid."""
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_settings(d: dict, path: Path = SETTINGS_PATH) -> None:
    """Write settings atomically; a failed write leaves the old file and is otherwise ignored."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2) + "\n")
        tmp.replace(path)
    except OSError:
        pass


def workout_status(x: dict) -> str:
    return "confirmed" if x["confirmed"] else "likely workout"


def zones_text(z: dict[str, int]) -> str:
    """'Z3 14 · Z4 4': minutes per zone, Z1 first, zero zones left out."""
    return " · ".join(f"{k} {v}" for k, v in z.items()) or "—"


def recovery_text(rec: dict) -> str:
    """HR drop 1 min after the effort end ('−59 bpm'), or '—' when workout@2 has none."""
    d = rec.get("drop_1min")
    return "—" if d is None else f"−{d} bpm" if d >= 0 else f"+{-d} bpm"


def workout_lines(x: dict, now: int | None = None) -> str:
    """The two lines above the workouts table for the newest workout (a workout_rows record)."""
    if x["confirmed"]:
        mins = f"{core.dur(x['duration_min'])} (confirmed; detected {core.dur(x['detected_min'])})"
    else:
        mins = f"≈{core.dur(x['duration_min'])} likely workout (elevated HR, not confirmed)"
    rec = recovery_text(x["recovery"])
    return (f"[b]Last workout[/]  {core.window_label(x['start_unix'], x['end_unix'], now)}  [dim]{x['id']}[/]\n"
            f"{mins} · avg {x['avg_hr']} / max {x['max_hr']} bpm (1-min means) · peak {x['peak_hr_5s']} (5 s)"
            + (f" · HR {rec} after 1 min" if rec != "—" else ""))


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def drain_bar(pct: int | None, frame: int, width: int = 28) -> str:
    """Filled portion = progress; a bright pulse sweeps through the rest so the bar visibly moves
    even when pct (the strap's own counter) advances slowly. pct None = indeterminate."""
    filled = 0 if pct is None else max(0, min(width, round(width * pct / 100)))
    rest = width - filled
    cells = ["█"] * filled + ["·"] * rest
    if rest:
        head = filled + frame % (rest + 4) - 2          # sweeps left→right, briefly off both ends
        for off, ch in ((0, "▓"), (-1, "▒"), (-2, "░"), (1, "▒")):
            i = head + off
            if filled <= i < width:
                cells[i] = ch
    return "▕" + "".join(cells) + "▏"


class WhoopApp(App):
    TITLE = "Whoop PoC"
    CSS = """
    Screen { padding: 0 1; }
    #meta { color: $text-muted; height: auto; margin-bottom: 1; }
    #live { height: auto; border: round $error; padding: 0 1; display: none; }
    #live-head, #live-chart, #live-key { height: auto; }
    #live-key { color: $text-muted; }
    #drain { height: auto; color: $accent; border: round $accent; padding: 0 1; display: none; }
    .h { text-style: bold; color: $warning; margin-top: 1; height: auto; }
    #tiles { height: 5; }
    Tile { border: round $panel-lighten-2; width: 1fr; height: 5; padding: 0 1; }
    #hr-chart { height: auto; margin-top: 1; }
    #hr-key { height: auto; color: $text-muted; }
    #hr-axis, #sleep-note, #hypno-legend, #workout-note { color: $text-muted; height: auto; }
    #workout-last { height: auto; margin-bottom: 1; }
    #hypno { color: $accent; height: auto; margin-top: 1; }
    DataTable { height: auto; max-height: 8; }
    /* ANSI themes: the header uses the terminal's own fg/bg pair, so it reads in any palette */
    DataTable:ansi > .datatable--header { background: ansi_default; color: ansi_default; text-style: bold underline; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("r", "reload", "Reload"), ("s", "sync", "Sync strap now"),
                ("w", "window", "Window 24h/3d/7d"), ("l", "live", "Live HR on/off"),
                ("plus", "extend_live", "+10 min live"),
                ("d", "toggle_dark", "Dark/light")]

    def __init__(self, args, pool=None) -> None:
        super().__init__()
        self.args = args
        self._frame = 0
        self._drain: dict | None = None
        self._was_draining = False
        self._live: dict | None = None
        self._live_pending = 0.0        # until when a just-requested live session counts as starting
        self._live_ours = False         # this TUI started it → stop it on quit (strap battery)
        self._live_stopping = None      # pid we asked to stop (it takes a few s to switch the stream off)
        self._live_drawn, self._live_drawn_at = False, 0.0
        self._report: dict | None = None          # the last /v1/report document
        self._status: dict | None = None          # the last /v1/status document
        self._live_doc: dict | None = None        # the last /v1/live document ...
        self._live_s: list[tuple[int, int]] = []  # ... and the session's samples so far
        self._threads = None
        self._client = make_client(args, pool)
        self._building = self._rebuild = False
        self._exiting = False
        self._max_hr = core.max_hr_info()
        self._settings = load_settings()
        hours = self._settings.get("hours")
        if not getattr(args, "hours_explicit", False) and hours in WINDOW_STEPS:
            self.args.hours = hours

    def save_setting(self, key: str, value) -> None:
        self._settings[key] = value
        save_settings(self._settings)

    def save_theme(self, theme) -> None:
        self.save_setting("theme", theme.name)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
            yield Static(id="meta")
            with Vertical(id="live"):
                yield Static(id="live-head")
                yield Static(id="live-chart")
                yield Static(id="live-key")
            yield Static(id="drain")
            yield Static("HEART RATE", classes="h")
            with Horizontal(id="tiles"):
                for i in ("now", "rest", "range", "worn", "batt"):
                    yield Tile(id=f"t-{i}")
            yield Static(id="hr-chart")
            yield Static(id="hr-axis")
            yield Static(id="hr-key")
            yield Static("SLEEP", id="sleep-head", classes="h")
            yield Static(id="sleep-note")
            yield DataTable(id="sleep-table", cursor_type="row", zebra_stripes=True)
            with Vertical():
                yield Static(id="hypno")
                yield Static(id="hypno-legend")
            yield Static("WORKOUTS", id="workout-head", classes="h")
            yield Static(id="workout-note")
            yield Static(id="workout-last")
            yield DataTable(id="workout-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        if self._settings.get("theme") in self.available_themes:
            self.theme = self._settings["theme"]
        self.theme_changed_signal.subscribe(self, self.save_theme)
        self.fetch_status()
        self.action_reload()
        self.set_interval(self.args.refresh, self.action_reload)
        self.poll_drain()
        self.set_interval(1.0, self.poll_drain)
        self.set_interval(0.1, self.animate_drain)

    def fetch_status(self) -> None:
        try:
            self._status = self._client.get("/v1/status")
        except api.ApiError:
            self._status = None
        self._drain = self._status["sync"]["progress"] if self._status else None

    def fetch_live(self) -> None:
        """The live session and its new samples (only the ones after the newest we have)."""
        q = {"session": self._live_doc["session_start_unix"], "since": self._live_s[-1][0]} \
            if self._live_s and self._live_doc else {}
        try:
            d = self._client.get("/v1/live", **q)
        except api.ApiError:
            return
        new = [tuple(x) for x in d["samples"]]
        self._live_s = new if d["full"] else self._live_s + new
        self._live_doc, self._live = d, d["running"]

    def post(self, path: str, **body) -> dict | None:
        """POST an action; an error is shown as a notification and returns None."""
        try:
            return self._client.post(path, **body)
        except api.ApiError as e:
            self.notify(e.message, severity="warning" if e.code == 409 else "error")
            return None

    def poll_drain(self) -> None:
        self.fetch_status()
        if self._drain:
            self._was_draining = True
            if self._frame % 100 < 10:          # ~every 10 s while draining: show data as it lands
                self.action_reload()
        elif self._was_draining:
            self._was_draining = False
            self.query_one("#drain", Static).display = False
            self.action_reload()
            self.notify("Drain finished — data reloaded")

    def action_live(self) -> None:
        if self._live or time.time() < self._live_pending:
            self._live_stopping = (self._live or {}).get("pid")
            self.post("/v1/live/stop")
            self._live_pending, self._live_ours = 0.0, False
            self.notify("Live HR off")
        elif (d := self.post("/v1/live/start")) is not None:
            self.notify(d["message"] + f" (auto-off after {d.get('minutes', core.LIVE_MINUTES):g} min)")
            self._live_pending, self._live_ours = time.time() + 60, d["started"]
        self.render_live()

    def action_extend_live(self) -> None:
        try:
            self._client.post("/v1/live/extend")
            self.notify("Live HR: +10 min")
        except api.ApiError as e:
            self.notify("Live HR isn't on (press l)" if e.code == 409 else e.message, severity="warning")

    async def action_quit(self) -> None:
        self._exiting = True
        if self._live_ours:                  # don't leave the strap streaming after we're gone
            try:
                self._client.post("/v1/live/stop")
            except api.ApiError:
                pass
        if self._client.pool is not None:    # don't wait for an in-flight build; the worker exits after it
            self._client.pool.shutdown(wait=False, cancel_futures=True)
        self.exit()

    def render_live(self) -> None:
        d, box = self._live, self.query_one("#live", Vertical)
        head, chart, key = (self.query_one(f"#live-{i}", Static) for i in ("head", "chart", "key"))
        pending = time.time() < self._live_pending
        if not d and not pending:
            last = (self._live_doc or {}).get("last")
            # After a session ends, keep its graph up for review (until the next one, or 12 h).
            if last and last.get("state") == "ended" and time.time() - (last.get("unix") or 0) < 12 * 3600:
                if self._render_live_graph(last, chart, key, ended=True):
                    s = self._live_s
                    bpms = [b for _, b in s]
                    head.update(f"[b]LAST LIVE SESSION[/]  {core.hhmm(s[0][0])} → {core.hhmm(s[-1][0])} · "
                                f"{core.dur((s[-1][0] - s[0][0]) // 60)} · avg [b]{sum(bpms) / len(bpms):.0f}[/] · "
                                f"max [b]{max(bpms)}[/] bpm    [dim]l starts a new one[/]")
                    box.display = True
                    return
            box.display = False
            return
        box.display = True
        if not d:
            head.update("[b red]♥ LIVE HR[/]  [dim]starting… (waiting for the strap; a running sync saves first)[/]")
            chart.update(""); key.update("")
            return
        if d.get("pid") == self._live_stopping:
            head.update("[red]♡[/] [b]LIVE HR[/]  [dim]stopping (switching the stream off)…[/]")
            return
        left = max(0, d["until_unix"] - int(time.time()))
        tail = f"[dim]auto-off in {left // 60}:{left % 60:02d} · + adds 10 min · l stops[/]"
        if d["state"] == "live" and d.get("bpm"):
            period = 60.0 / max(30, d["bpm"])
            beat = (time.time() % period) / period < 0.3
            heart = "[b red]♥[/]" if beat else "[red]♡[/]"
            stale = int(time.time()) - (d.get("unix") or 0)
            age = f"  [dim]({stale}s old)[/]" if stale > 5 else ""
            z = core.hr_zone(d["bpm"])
            head.update(f"{heart} [b]LIVE[/]  [b red]{d['bpm']}[/] bpm  [b {z[3]}]{z[0]} {z[1]}[/]{age}    {tail}")
        else:
            msg = {"connecting": "connecting to strap…", "waiting": "connected, waiting for heart rate (on your wrist?)",
                   "reconnecting": "link dropped, reconnecting…"}.get(d["state"], d["state"])
            head.update(f"[red]♡[/] [b]LIVE HR[/]  {msg}    {tail}")
        self._render_live_graph(d, chart, key, ended=False)

    def zone_key(self, secs: dict[str, int]) -> str:
        """Color key: each zone's swatch, bpm range and time in it (m:ss under an hour, else XhYYm)."""
        def t(v):
            return f"{v // 60}:{v % 60:02d}" if v < 3600 else f"{v // 3600}h{v % 3600 // 60:02d}m"
        return "   ".join(f"[{z[3]}]█[/] {z[0]} {core.zone_range(z)} [b]{t(secs[z[0]])}[/]"
                          for z in (core.REST_ZONE,) + core.ZONES[::-1]) + \
            f"   [dim]HR reserve: rest {core.REST_HR}, {core.max_hr_text(self._max_hr)}[/]"

    def _render_live_graph(self, d: dict, chart: Static, key: Static, ended: bool) -> bool:
        """Draw the session graph + zone key; True if there was enough data to draw."""
        if self._live_drawn and time.time() - self._live_drawn_at < 1.0:   # ~1/s; the heart animates faster
            return True
        s = self._live_s
        self._live_drawn_at = time.time()
        if len(s) < 2:
            chart.update(""); key.update("")
            self._live_drawn = False
            return False
        width = max(20, self.size.width - 8)
        chart.update("\n".join(core.live_chart(s, width, 6, t1=None if ended else int(time.time()))))
        key.update(self.zone_key(core.zone_seconds(s)))
        self._live_drawn = True
        return True

    def animate_drain(self) -> None:
        if self._frame % 3 == 0:
            self.fetch_live()
            if self._live:
                self._live_pending = 0.0
            elif self._live_ours and time.time() >= self._live_pending:
                self._live_ours = False          # it ended (time limit / dropped)
        self.render_live()
        d, w = self._drain, self.query_one("#drain", Static)
        self._frame += 1
        if not d:
            return
        spin = SPINNER[self._frame % len(SPINNER)]
        label = {"draining": "DRAINING STRAP", "connecting": "CONNECTING TO STRAP",
                 "reconnecting": "RECONNECTING (BLE dropped, resuming)", "finishing": "SAVING + DECODING"}[d["phase"]]
        bits = []
        if d["at"]:
            bits.append(f"synced through [b]{d['at'][:-3]}[/]")
        if d["rate"] is not None:
            bits.append(f"{d['rate']:.0f} rec/s")
        if d["eta"] and d["eta"] != "—":
            bits.append(f"ETA {d['eta']}")
        if d["stored"] is not None:
            bits.append(f"{d['stored']:,} stored")
        pct = f" {d['pct']:>3}%" if d["pct"] is not None else ""
        w.update(f"[b]{spin} {label}[/]  {drain_bar(d['pct'], self._frame)}{pct}\n[dim]{' · '.join(bits)}[/]")
        w.display = True

    def action_reload(self) -> None:
        """Fetch /v1/report on a thread. The client asks the API server, or builds it in the persistent
        worker process (0.3–4 s of decoding + NOOP's stager; a thread wasn't enough, the decoding holds
        Python's GIL and stalled the UI up to ~0.9 s). Requests during a build coalesce into one more."""
        if self._building:
            self._rebuild = True
            return
        self._building, self._rebuild = True, False
        hours = self.args.hours
        self.render_meta_loading()
        if self._threads is None:
            from concurrent.futures import ThreadPoolExecutor
            self._threads = ThreadPoolExecutor(1)
        fut = self._threads.submit(self._client.get, "/v1/report", hours=hours, bins=self.chart_cols())

        def done(f) -> None:                    # runs on the pool's thread: hand back to the UI
            if self._exiting or not self.is_running or f.cancelled():
                return                          # quit mid-build: nothing left to hand the result to
            try:
                r = f.result()
            except BaseException as e:
                r = e
            try:
                self.call_from_thread(self._report_ready, r, hours)
            except RuntimeError:                # app already exited
                pass

        fut.add_done_callback(done)

    def _report_ready(self, r, hours) -> None:
        self._building = False
        if isinstance(r, BaseException):
            self.notify(f"Reload failed: {getattr(r, 'message', r)}", severity="error")
        elif hours == self.args.hours:          # drop a result for a window the user already left
            self._report = r
            self.render_report(r)
        if self._rebuild or hours != self.args.hours:
            self.action_reload()

    def render_meta_loading(self) -> None:
        win = (self._report or {}).get("window")
        if not win or win["until_unix"] - win["since_unix"] != int(self.args.hours * 3600):
            self.query_one("#meta", Static).update(
                f"[b]loading last {core.dur(int(self.args.hours * 60)).replace(' 00m', '')}…[/]")

    def on_resize(self, _event) -> None:
        # Width-dependent pieces (sleep timeline, hour axis) were sized for the old width: redraw now
        # instead of wrapping until the next auto-reload.
        # self.size still holds the old width while this handler runs: redraw after the refresh.
        if getattr(self, "_report", None) is not None:
            self.call_after_refresh(self._redraw_resized)
        self._live_drawn = False

    def _redraw_resized(self) -> None:
        self.render_report(self._report)
        if self._report["hr"]["series"]["bins"] != self.chart_cols():   # sliced for the old chart width
            self.action_reload()

    def action_sync(self) -> None:
        # Never own the drain: it can run 20 min, and q must quit immediately. The banner tracks it.
        if (d := self.post("/v1/sync")) is not None:
            self.notify(d["message"] + " (strap must be near the laptop)")
        self.poll_drain()

    def action_window(self) -> None:
        self.args.hours = next((h for h in WINDOW_STEPS if h > self.args.hours), WINDOW_STEPS[0])
        self.save_setting("hours", self.args.hours)
        self.notify(f"Window: last {core.dur(int(self.args.hours * 60)).replace(' 00m', '')}")
        self.action_reload()

    def chart_cols(self) -> int:
        """Bar columns in the HR chart (its width less the 4-character bpm labels): /v1/report's bins."""
        return max(20, self.size.width - 6) - 4

    def render_report(self, doc: dict) -> None:
        self._max_hr = doc["max_hr"]
        core.MAX_HR = doc["max_hr"]["max_hr"]     # zone colors and ranges follow the report's max_hr
        since, until = doc["window"]["since_unix"], doc["window"]["until_unix"]
        newest = doc["newest_unix"]
        self.sub_title = doc["device"]["name"] or doc["device"]["address"]
        via = f" · [dim]via API :{self._client.port}[/]" if self._client.mode == "api" else ""
        self.query_one("#meta", Static).update(
            f"window: last {(until - since) // 3600}h [dim](w to change)[/] · newest data "
            f"{core.stamp(newest or 0)}"
            f"{' (' + core.ago(newest) + ')' if newest else ''} · "
            f"auto-reload {self.args.refresh}s · "
            f"times in {datetime.now().astimezone().tzname()}{via}")

        b, bt = (self._status or {}).get("battery"), self.query_one("#t-batt", Tile)
        if b:
            color = "red" if b["pct"] <= 15 else "yellow" if b["pct"] <= 30 else "green"
            bt.show("STRAP BATTERY", f"[{color}]{b['pct']}%[/]", f"read {core.ago(b['unix'])}")
        else:
            bt.show("STRAP BATTERY", "—", "read after next sync")

        hr = doc["hr"]
        now, stats, series = hr["latest"], hr["stats"], hr["series"]
        t = {k: self.query_one(f"#t-{k}", Tile) for k in ("now", "rest", "range", "worn")}
        chart, axis, key = (self.query_one(f"#hr-{i}", Static) for i in ("chart", "axis", "key"))
        if now and stats:
            t["now"].show("♥ LATEST", f"[red]{now['bpm']}[/] bpm", f"{core.hhmm(now['unix'])} · {core.ago(now['unix'])}")
            t["rest"].show("RESTING ≈", f"{stats['rest']:.0f} bpm", "5th pct, still minutes")
            t["range"].show("RANGE", f"{stats['min']:.0f}–{stats['max']:.0f} bpm", f"avg {stats['avg']:.0f}")
            t["worn"].show("WORN", core.dur(stats["worn_min"]), f"last {(until - since) // 3600}h")
            # Time-true: each point sits at its real time (gaps = strap off), columns show the peak of
            # their slice (a 30-min run must not average away at 7d), bars colored by HR zone.
            width = max(20, self.size.width - 6)
            rows = core.zone_chart([tuple(p) for p in series["points"]], width, 6, t0=since, t1=until,
                                   agg="max", gap_fill_s=61)
            ticks, labels = core.time_axis(since, until, width - 4)
            chart.update("\n".join(rows + [f"[dim]    {ticks}\n    {labels}[/]"]))
            axis.update(f"per-minute HR, peak per column  ({stats['min']:.0f}–{stats['max']:.0f} bpm)")
            key.update(self.zone_key(hr["zone_sec"]))
        else:
            for k in t:
                t[k].show(k.upper(), "—", "")
            chart.update("")
            key.update("")
            d = self._drain
            axis.update(f"nothing in this window yet: the drain has reached {d['at'][:-3]} and fills in "
                        f"oldest → newest. Press w for a longer window." if d and d.get("at") else
                        "no heart-rate data in this window: wear the strap, then press s to sync"
                        if not self._drain else "nothing in this window yet: waiting for the drain to start")

        self.render_workouts(doc["workouts"])
        sl = doc["sleep"]
        staged = sl["source"] == "noop"
        self.query_one("#sleep-head", Static).update(f"SLEEP  [dim]{core.SLEEP_SOURCE_LABEL[sl['source']]}[/]")
        table = self.query_one("#sleep-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Window", "In bed", "Asleep ≈", "Eff.", "Avg HR", "Low HR",
                          *(("Deep", "Light", "REM", "Wake", "Rest HR", "HRV") if staged else ()))
        note, hyp, legend = (self.query_one(f"#{i}", Static) for i in ("sleep-note", "hypno", "hypno-legend"))
        if not sl["sessions"]:
            note.update(sl["note"])
            hyp.update("")
            legend.update("")
            table.display = False
            return
        note.update("")
        table.display = True
        for s in reversed(sl["sessions"]):
            sm = s["stage_min"] or {k: 0 for k in core.STAGES}
            table.add_row(f"{core.stamp(s['start_unix'])} → {core.hhmm(s['end_unix'])}", core.dur(s["in_bed_min"]),
                          core.dur(s["asleep_min"]), f"{100 * s['asleep_min'] / max(1, s['in_bed_min']):.0f}%",
                          "—" if s["avg_hr"] is None else f"{s['avg_hr']:.0f}",
                          "—" if s["low_hr"] is None else f"{s['low_hr']:.0f}",
                          *((*(core.dur(sm[k]) for k in ("deep", "light", "rem", "wake")), str(s["resting_hr"] or "—"),
                             f"{s['avg_hrv_ms']:.0f} ms" if s["avg_hrv_ms"] else "—") if staged else ()))
        last = api.sleep_window(sl["last"])
        width = max(20, self.size.width - 8)
        if staged:
            bar = core.stage_bar(last, width, markup=True)
            legend_txt = "   ".join(f"[{core.STAGE_COLOR[k]}]{core.STAGE_CHAR[k]}[/] {k}" for k in core.STAGES)
        else:
            bins = core.bucket(last.minutes, last.start, last.end, width)
            bar = "".join(" " if not b else "█" if sum(m.asleep for m in b) * 2 >= len(b) else "[dim]░[/]"
                          for b in bins)
            legend_txt = "█ still + calm HR (≈asleep)   ░ restless / awake"
        ticks, labels = core.hour_axis(last.start, last.end, width, ends=True)
        if width >= 12:                     # exact start/end bold, hour markers dim
            labels = f"[b]{labels[:5]}[/][dim]{labels[5:-5]}[/][b]{labels[-5:]}[/]"
        hyp.update(f"[b]Last sleep[/]  {core.window_label(last.start, last.end)}\n"
                   f"{core.sleep_hours(last)}\n{bar}\n[dim]{ticks}[/]\n{labels}")
        legend.update(legend_txt)

    def render_workouts(self, xs: list[dict]) -> None:
        self.query_one("#workout-head", Static).update(
            "WORKOUTS  [dim](detected from elevated HR; confirm: whoop-poc coach confirm <id> --minutes N)[/]")
        note, last = self.query_one("#workout-note", Static), self.query_one("#workout-last", Static)
        table = self.query_one("#workout-table", DataTable)
        table.clear(columns=True)
        if not xs:
            note.update(f"No workouts in this window: needs ≥{coach.WORKOUT_MIN} min at "
                        f"{coach.WORKOUT_ZONE[0]}+ HR (≥{core.zone_low(coach.WORKOUT_ZONE):.0f} bpm). Press w for a longer window.")
            last.update("")
            table.display = False
            return
        note.update("")
        last.update(workout_lines(xs[-1]))
        table.display = True
        table.add_columns("ID", "Status", "Duration", "Avg HR", "Max 1m", "Peak 5s", "Zones (min)", "HR drop 1m")
        for x in reversed(xs):
            table.add_row(x["id"], workout_status(x), core.dur(x["duration_min"]), str(x["avg_hr"]),
                          str(x["max_hr"]), str(x["peak_hr_5s"] or "—"), zones_text(x["zone_min"]), recovery_text(x["recovery"]))


def make_client(args, pool=None) -> Client:
    """The TUI's API client. A run the shared server doesn't serve (--demo, another --db, --address,
    --max-hr) stays in-process."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(db=args.db, address=args.address, demo=args.demo, heuristic=False,
                          days=getattr(args, "days", coach.COACH_DAYS))
    shared = not (args.demo or args.address or getattr(args, "max_hr", None) is not None
                  or Path(args.db).expanduser().resolve() != core.DEFAULT_DB.resolve())
    return Client(cfg, api.port(args), pool, use_api=shared)


def make_report_pool():
    """One-worker process pool for report builds, started BEFORE Textual takes over stdout/stderr
    (spawning from inside a running app fails: Textual's stream stand-ins have no file descriptor).
    The worker is spawned eagerly so later submits need no new process. None if it can't start."""
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    try:
        pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
        pool.submit(int).result(timeout=20)     # spawn + import the worker now
        return pool
    except Exception:
        return None


def run_tui(args) -> None:
    args.hours_explicit = any(a == "--hours" or a.startswith("--hours=") for a in sys.argv[1:])
    pool = make_report_pool()
    try:
        WhoopApp(args, pool).run()
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

"""Textual TUI for whoop-poc: heart rate + approximate sleep from a local NOOP whoop.db."""
from __future__ import annotations

import statistics as st
import time
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Sparkline, Static

import whoop_poc as core


class Tile(Static):
    def show(self, label: str, value: str, note: str = "") -> None:
        self.update(f"[dim]{label}[/]\n[b]{value}[/]\n[dim]{note}[/]")


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
    #hr-spark { height: 6; margin-top: 1; }
    #hr-spark > .sparkline--max-color { color: $error; }
    #hr-spark > .sparkline--min-color { color: $error 40%; }
    #hr-axis, #sleep-note, #hypno-legend { color: $text-muted; height: auto; }
    #hypno { color: $accent; height: auto; margin-top: 1; }
    DataTable { height: auto; max-height: 8; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("r", "reload", "Reload"), ("s", "sync", "Sync strap now"),
                ("w", "window", "Window 24h/3d/7d"), ("l", "live", "Live HR on/off"),
                ("plus", "extend_live", "+10 min live"),
                ("d", "toggle_dark", "Dark/light")]

    def __init__(self, args) -> None:
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
            yield Sparkline([], id="hr-spark", summary_function=max)
            yield Static(id="hr-axis")
            yield Static("SLEEP", id="sleep-head", classes="h")
            yield Static(id="sleep-note")
            yield DataTable(id="sleep-table", cursor_type="row", zebra_stripes=True)
            with Vertical():
                yield Static(id="hypno")
                yield Static(id="hypno-legend")
        yield Footer()

    def on_mount(self) -> None:
        self._drain = None if self.args.demo else core.drain_status()
        self.action_reload()
        self.set_interval(self.args.refresh, self.action_reload)
        self.poll_drain()
        self.set_interval(1.0, self.poll_drain)
        self.set_interval(0.1, self.animate_drain)

    def poll_drain(self) -> None:
        self._drain = None if self.args.demo else core.drain_status()
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
            core.stop_live()
            self._live_pending, self._live_ours = 0.0, False
            self.notify("Live HR off")
        else:
            self.notify(core.start_live() + f" (auto-off after {core.LIVE_MINUTES} min)")
            self._live_pending, self._live_ours = time.time() + 60, True
        self.render_live()

    def action_extend_live(self) -> None:
        if core.extend_live():
            self.notify("Live HR: +10 min")
        else:
            self.notify("Live HR isn't on (press l)", severity="warning")

    async def action_quit(self) -> None:
        if self._live_ours:                  # don't leave the strap streaming after we're gone
            core.stop_live()
        self.exit()

    def render_live(self) -> None:
        d, box = self._live, self.query_one("#live", Vertical)
        head, chart, key = (self.query_one(f"#live-{i}", Static) for i in ("head", "chart", "key"))
        pending = time.time() < self._live_pending
        if not d and not pending:
            last = None if self.args.demo else core.last_live()
            # After a session ends, keep its graph up for review (until the next one, or 12 h).
            if last and last.get("state") == "ended" and time.time() - (last.get("unix") or 0) < 12 * 3600:
                if self._render_live_graph(last, chart, key, ended=True):
                    s = core.live_samples()
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

    def _render_live_graph(self, d: dict, chart: Static, key: Static, ended: bool) -> bool:
        """Draw the session graph + zone key; True if there was enough data to draw."""
        if self._live_drawn and time.time() - self._live_drawn_at < 1.0:   # ~1/s; the heart animates faster
            return True
        s = core.live_samples()
        self._live_drawn_at = time.time()
        if len(s) < 2:
            chart.update(""); key.update("")
            self._live_drawn = False
            return False
        width = max(20, self.size.width - 8)
        chart.update("\n".join(core.live_chart(s, width, 6, t1=None if ended else int(time.time()))))
        secs = core.zone_seconds(s)
        key.update("   ".join(
            f"[{z[3]}]█[/] {z[0]} {core.zone_range(z)} [b]{secs[z[0]] // 60}:{secs[z[0]] % 60:02d}[/]"
            for z in (core.REST_ZONE,) + core.ZONES[::-1]) + f"   [dim]max HR {core.MAX_HR} (WHOOP_MAX_HR)[/]")
        self._live_drawn = True
        return True

    def animate_drain(self) -> None:
        if self._frame % 3 == 0:
            self._live = None if self.args.demo else core.live_status()
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
        r = core.build_report(self.args.db, self.args.hours, self.args.address, demo=self.args.demo)
        self.render_report(r)

    def action_sync(self) -> None:
        # Never own the drain: it can run 20 min, and q must quit immediately. The banner tracks it.
        ok, msg = core.start_sync_background()
        self.notify(msg + (" (strap must be near the laptop)" if ok else ""),
                    severity="information" if ok else "error")
        self.poll_drain()

    def action_window(self) -> None:
        steps = (24, 72, 168)
        self.args.hours = next((h for h in steps if h > self.args.hours), steps[0])
        self.notify(f"Window: last {core.dur(int(self.args.hours * 60)).replace(' 00m', '')}")
        self.action_reload()

    def render_report(self, r: core.Report) -> None:
        self.sub_title = r.meta.get("name") or r.meta["address"]
        self.query_one("#meta", Static).update(
            f"window: last {(r.until - r.since) // 3600}h [dim](w to change)[/] · newest data "
            f"{core.stamp(r.meta.get('newest_unix') or 0)}"
            f"{' (' + core.ago(r.meta['newest_unix']) + ')' if r.meta.get('newest_unix') else ''} · "
            f"auto-reload {self.args.refresh}s · "
            f"times in {datetime.now().astimezone().tzname()}")

        b, bt = (None if self.args.demo else core.last_battery()), self.query_one("#t-batt", Tile)
        if b:
            color = "red" if b["pct"] <= 15 else "yellow" if b["pct"] <= 30 else "green"
            bt.show("STRAP BATTERY", f"[{color}]{b['pct']}%[/]", f"read {core.ago(b['unix'])}")
        else:
            bt.show("STRAP BATTERY", "—", "read after next sync")

        now, stats, hm = r.hr_now, r.hr_stats(), r.hr_minutes
        t = {k: self.query_one(f"#t-{k}", Tile) for k in ("now", "rest", "range", "worn")}
        spark, axis = self.query_one("#hr-spark", Sparkline), self.query_one("#hr-axis", Static)
        if now and stats:
            t["now"].show("♥ LATEST", f"[red]{now.hr}[/] bpm", f"{core.hhmm(now.unix)} · {core.ago(now.unix)}")
            t["rest"].show("RESTING ≈", f"{stats['rest']:.0f} bpm", "5th pct, still minutes")
            t["range"].show("RANGE", f"{stats['min']:.0f}–{stats['max']:.0f} bpm", f"avg {stats['avg']:.0f}")
            t["worn"].show("WORN", core.dur(stats["worn_min"]), f"last {(r.until - r.since) // 3600}h")
            spark.data = [m.hr for m in hm]
            axis.update(f"per-minute HR  {core.hhmm(hm[0].t)} → {core.hhmm(hm[-1].t + 60)}   "
                        f"({min(m.hr for m in hm):.0f}–{max(m.hr for m in hm):.0f} bpm)")
        else:
            for k in t:
                t[k].show(k.upper(), "—", "")
            spark.data = []
            d = self._drain
            axis.update(f"nothing in this window yet: the drain has reached {d['at'][:-3]} and fills in "
                        f"oldest → newest. Press w for a longer window." if d and d.get("at") else
                        "no heart-rate data in this window: wear the strap, then press s to sync"
                        if not self._drain else "nothing in this window yet: waiting for the drain to start")

        staged = r.meta.get("sleep_source") == "noop"
        self.query_one("#sleep-head", Static).update(
            f"SLEEP  [dim]{core.SLEEP_SOURCE_LABEL[r.meta.get('sleep_source', 'heuristic')]}[/]")
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_columns("Window", "In bed", "Asleep ≈", "Eff.", "Avg HR", "Low HR",
                          *(("Deep", "Light", "REM", "Wake", "Rest HR", "HRV") if staged else ()))
        note, hyp, legend = (self.query_one(f"#{i}", Static) for i in ("sleep-note", "hypno", "hypno-legend"))
        if not r.sleep:
            note.update(core.no_sleep_msg(r))
            hyp.update("")
            legend.update("")
            table.display = False
            return
        note.update("")
        table.display = True
        for s in reversed(r.sleep):
            hrs = s.hrs
            table.add_row(f"{core.stamp(s.start)} → {core.hhmm(s.end)}", core.dur(s.span_min),
                          core.dur(s.asleep_min), f"{100 * s.asleep_min / max(1, s.span_min):.0f}%",
                          f"{st.mean(hrs):.0f}" if hrs else "—", f"{min(hrs):.0f}" if hrs else "—",
                          *((*(core.dur(v) for v in (lambda m: (m["deep"], m["light"], m["rem"], m["wake"]))(
                              s.stage_minutes())), str(s.resting_hr or "—"),
                             f"{s.avg_hrv:.0f} ms" if s.avg_hrv else "—") if staged else ()))
        last = r.sleep[-1]
        width = max(20, self.size.width - 8)
        if staged:
            bar = core.stage_bar(last, width, markup=True)
            legend_txt = "   ".join(f"[{core.STAGE_COLOR[k]}]{core.STAGE_CHAR[k]}[/] {k}" for k in core.STAGES)
        else:
            bins = core.bucket(last.minutes, last.start, last.end, width)
            bar = "".join(" " if not b else "█" if sum(m.asleep for m in b) * 2 >= len(b) else "[dim]░[/]"
                          for b in bins)
            legend_txt = "█ still + calm HR (≈asleep)   ░ restless / awake"
        hyp.update(f"latest window  {core.window_label(last.start, last.end)}\n{bar}")
        legend.update(legend_txt)


def run_tui(args) -> None:
    WhoopApp(args).run()

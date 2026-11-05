"""Textual TUI for whoop-poc: heart rate + approximate sleep from a local NOOP whoop.db."""
from __future__ import annotations

import statistics as st
import time
from datetime import datetime

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Sparkline, Static

import whoop_poc as core


class Tile(Static):
    def show(self, label: str, value: str, note: str = "") -> None:
        self.update(f"[dim]{label}[/]\n[b]{value}[/]\n[dim]{note}[/]")


class WhoopApp(App):
    TITLE = "Whoop PoC"
    CSS = """
    Screen { padding: 0 1; }
    #meta { color: $text-muted; height: auto; margin-bottom: 1; }
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
                ("d", "toggle_dark", "Dark/light")]

    def __init__(self, args) -> None:
        super().__init__()
        self.args = args

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
            yield Static(id="meta")
            yield Static("HEART RATE", classes="h")
            with Horizontal(id="tiles"):
                for i in ("now", "rest", "range", "worn"):
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
        self.action_reload()
        self.set_interval(self.args.refresh, self.action_reload)

    def action_reload(self) -> None:
        r = core.build_report(self.args.db, self.args.hours, self.args.address, demo=self.args.demo)
        self.render_report(r)

    @work(thread=True, exclusive=True)
    def action_sync(self) -> None:
        self.call_from_thread(self.notify, "Syncing… (strap must be near the laptop)")
        ok = core.run_sync(quiet=True)
        self.call_from_thread(self.notify, "Sync done" if ok else "Sync failed — try `whoop-sync sync`",
                              severity="information" if ok else "error")
        self.call_from_thread(self.action_reload)

    def render_report(self, r: core.Report) -> None:
        self.sub_title = r.meta.get("name") or r.meta["address"]
        self.query_one("#meta", Static).update(
            f"window: last {(r.until - r.since) // 3600}h · last sync {core.stamp(r.meta['last_sync'])} "
            f"({core.ago(r.meta['last_sync'])}) · auto-reload {self.args.refresh}s · "
            f"times in {datetime.now().astimezone().tzname()}")

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
            axis.update("no heart-rate data in this window — wear the strap, then press s to sync")

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
        hyp.update(f"latest window  {core.hhmm(last.start)} → {core.hhmm(last.end)}\n{bar}")
        legend.update(legend_txt)


def run_tui(args) -> None:
    WhoopApp(args).run()

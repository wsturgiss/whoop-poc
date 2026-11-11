"""Tests use synthetic frames/samples only — no real biometrics in the repo."""
import struct
import unittest

import whoop_poc as w


def make_frame(unix, hr, g=(0.1, -0.4, 0.91), version=12):
    b = bytearray(96)
    b[0], b[4], b[5] = 0xAA, w.TYPE_HISTORICAL, version
    struct.pack_into("<I", b, w.OFF_UNIX, unix)
    b[w.OFF_HR] = hr
    struct.pack_into("<3f", b, w.OFF_GRAV, *g)
    return bytes(b)


class Decode(unittest.TestCase):
    def test_v12_fields(self):
        s = w.decode_frame(make_frame(1793853476, 67))
        self.assertEqual((s.unix, s.hr), (1793853476, 67))
        self.assertAlmostEqual(s.gz, 0.91, places=5)

    def test_rejects_bad_gravity_and_versions(self):
        self.assertIsNone(w.decode_frame(make_frame(1, 60, g=(0, 0, 0))))
        self.assertIsNone(w.decode_frame(make_frame(1, 60, version=26)))


class Sleep(unittest.TestCase):
    END = 1793880000

    def test_detects_synthetic_night(self):
        mins = w.per_minute(w.demo_samples(self.END))
        sleep = w.detect_sleep(mins)
        self.assertEqual(len(sleep), 1, [(s.start, s.end) for s in sleep])
        s = sleep[0]
        self.assertLess(abs(s.start - (self.END - 8 * 3600)), 10 * 60)
        self.assertLess(abs(s.end - (self.END - 30 * 60)), 10 * 60)
        self.assertGreater(s.asleep_min, 6.5 * 60)
        self.assertLess(max(s.hrs), 65)

    def test_awake_only_has_no_sleep(self):
        awake = [s for s in w.demo_samples(self.END) if s.unix < self.END - 8 * 3600]
        self.assertEqual(w.detect_sleep(w.per_minute(awake)), [])

    def test_still_evening_wear_is_not_sleep(self):
        # 50 min sitting still at a desk, HR only slightly below a short active stretch -> not sleep
        import random
        rnd, out, t0 = random.Random(1), [], self.END - 3 * 3600
        for i in range(15 * 60):                      # 15 min moving, HR ~72
            out.append(w.Sample(t0 + i, 72 + int(rnd.gauss(0, 2)), 0.1 + rnd.gauss(0, .2), -0.4, 0.9))
        for i in range(15 * 60, 65 * 60):             # 50 min still, HR ~66
            out.append(w.Sample(t0 + i, 66 + int(rnd.gauss(0, 1.5)), 0.1, -0.4, 0.91))
        self.assertEqual(w.detect_sleep(w.per_minute(out)), [])

    def test_awake_label_overrides(self):
        samples = w.demo_samples(self.END)
        lab = [(self.END - 9 * 3600, self.END, "awake")]
        self.assertEqual(w.detect_sleep(w.per_minute(samples), lab), [])

    def test_demo_is_pinned_far_from_now(self):
        r = w.build_report(None, 24, None, demo=True)
        self.assertLess(r.until, 1600000000)
        self.assertIn("NOT your night", r.meta["name"])

    def test_renderers(self):
        r = w.build_report(None, 24, None, now=self.END, demo=True, heuristic=True)
        self.assertIn("asleep", "\n".join(t for _, t in w.text_lines(r, 100)))
        self.assertIn('<rect class="band"', w.render_html(r))


class Stages(unittest.TestCase):
    END = 1793880000

    def fake_window(self):
        st = [{"start": 0, "end": 1800, "stage": "light"}, {"start": 1800, "end": 3000, "stage": "deep"},
              {"start": 3000, "end": 3600, "stage": "rem"}, {"start": 3600, "end": 3900, "stage": "wake"}]
        return w.SleepWindow(0, 3900, [], stages=st, resting_hr=50, avg_hrv=60.0)

    def test_stage_accounting(self):
        s = self.fake_window()
        self.assertEqual(s.stage_minutes(), {"wake": 5, "rem": 10, "light": 30, "deep": 20})
        self.assertEqual(s.asleep_min, 60)
        self.assertEqual(w.stage_bar(s, 13), "▅▅▅▅▅▅████▃▃·")

    def test_html_hypnogram(self):
        r = w.build_report(None, 24, None, now=self.END, demo=True, heuristic=True)
        r.meta["sleep_source"], r.sleep = "noop", [self.fake_window()]
        html = w.render_html(r)
        self.assertIn('aria-label="Sleep stages"', html)
        self.assertIn("<th>Deep</th>", html)

    @unittest.skipUnless(w.STAGER_BIN.exists(), "stager not built (cd stager && ./build.sh)")
    def test_noop_stager_runs_on_demo_night(self):
        r = w.build_report(None, 24, None, now=self.END, demo=True)
        self.assertEqual(r.meta["sleep_source"], "noop")
        self.assertTrue(r.sleep)
        self.assertGreater(sum(s.asleep_min for s in r.sleep), 6 * 60)
        self.assertTrue(all(g["stage"] in w.STAGES for s in r.sleep for g in s.stages))


if __name__ == "__main__":
    unittest.main()


class Snapshot(unittest.TestCase):
    """The Coach contract (COACH.md): keys and types must stay stable within a schema version."""

    def test_heuristic_demo_snapshot_shape(self):
        import json, os, tempfile
        from pathlib import Path
        r = w.build_report(Path("/nonexistent"), 24, None, demo=True, heuristic=True)
        doc = json.loads(json.dumps(w.snapshot(r, now=w.DEMO_END)))
        self.assertEqual(doc["schema"], "whoop-poc/snapshot@1")
        self.assertTrue(doc["demo"])
        self.assertEqual(set(doc), {"schema", "demo", "generated_unix", "generated_iso", "window", "device",
                                    "freshness", "hr", "sleep", "battery", "labels"})
        self.assertIsNone(doc["battery"])              # demo never reports the real strap's battery
        self.assertEqual(doc["hr"]["latest"]["unix"], doc["freshness"]["newest_sample_unix"])
        self.assertEqual(doc["sleep"]["source"], "heuristic")
        s = doc["sleep"]["sessions"][0]
        self.assertIsNone(s["stages"])
        self.assertGreater(s["asleep_min"], 6 * 60)
        t, bpm = doc["hr"]["per_minute"][0]
        self.assertIsInstance(t, int)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "coach.json"
            w.write_json(p, doc)
            self.assertEqual(json.loads(p.read_text())["schema"], w.SNAPSHOT_SCHEMA)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)


class Drain(unittest.TestCase):
    def test_bar_moves_and_fills(self):
        from tui import drain_bar
        a, b = drain_bar(50, 0, 20), drain_bar(50, 3, 20)
        self.assertEqual(len(a), 22)
        self.assertTrue(a.startswith("▕" + "█" * 10))
        self.assertNotEqual(a, b)                      # the pulse moves between frames
        self.assertEqual(drain_bar(100, 5, 20), "▕" + "█" * 20 + "▏")

    def test_parses_progress_log(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "last-sync.log").write_text(
                "connected: True\n  sync 4C   63% ▕██████░░░░▏ @11-05 00:09:33  11.1 rec/s  ETA 1m45s  stored 2,254\n")
            with mock.patch.object(w, "STATE_DIR", Path(d)), mock.patch.object(w, "drain_running", return_value=True):
                s = w.drain_status()
            self.assertEqual((s["phase"], s["pct"], s["at"], s["stored"]), ("draining", 63, "11-05 00:09:33", 2254))
            with mock.patch.object(w, "drain_running", return_value=False):
                self.assertIsNone(w.drain_status())


class Battery(unittest.TestCase):
    def test_reads_and_rejects(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, mock.patch.object(w, "STATE_DIR", Path(d)):
            self.assertIsNone(w.last_battery())
            (Path(d) / "battery.json").write_text('{"schema":1,"pct":100,"unix":1793900000,"source":"bluez-battery-service"}')
            self.assertIsNone(w.last_battery())        # standard BLE service reading: untrustworthy
            (Path(d) / "battery.json").write_text('{"schema":1,"pct":67,"unix":1793900000,"source":"whoop-cmd-26"}')
            self.assertEqual(w.last_battery()["pct"], 67)
            (Path(d) / "battery.json").write_text('{"pct":"oops"}')
            self.assertIsNone(w.last_battery())


class Zones(unittest.TestCase):
    def test_zone_boundaries_and_labels(self):
        self.assertEqual(w.hr_zone(123, 185, 63)[0], "Z0")
        self.assertEqual(w.hr_zone(124, 185, 63)[0], "Z1")
        self.assertEqual(w.hr_zone(136, 185, 63)[0], "Z2")
        self.assertEqual(w.hr_zone(147.6, 185, 63)[0], "Z2")
        self.assertEqual(w.hr_zone(148, 185, 63)[0], "Z3")
        self.assertEqual(w.hr_zone(173, 185, 63)[0], "Z5")
        labels = [w.zone_range(z, 185, 63) for z in (w.REST_ZONE,) + w.ZONES[::-1]]
        self.assertEqual(labels, ["<124", "124–135", "136–147", "148–160", "161–172", "173+"])

    def test_color_and_label_agree_at_every_bpm(self):
        for mx, rest in ((185, 63), (190, 60), (185, 0), (201, 47)):
            for bpm in range(30, 220):
                z = w.hr_zone(bpm, mx, rest)
                r = w.zone_range(z, mx, rest)
                if r.startswith("<"):
                    self.assertLess(bpm, int(r[1:]))
                elif r.endswith("+"):
                    self.assertGreaterEqual(bpm, int(r[:-1]))
                else:
                    lo, hi = map(int, r.split("–"))
                    self.assertTrue(lo <= bpm <= hi, (mx, rest, bpm, r))

    def test_zone_seconds_and_chart(self):
        s = [(1000 + i, 100 if i < 60 else 160) for i in range(120)]
        secs = w.zone_seconds(s, 190, 0)
        self.assertEqual((secs["Z1"], secs["Z4"]), (60, 60))
        lines = w.live_chart(s, 40, 4, 190, rest_hr=0)
        self.assertEqual(len(lines), 5)                    # 4 bar rows + time axis
        self.assertIn(w.ZONES[1][3], lines[0])             # top row only has the Z4 (hard) bars
        self.assertNotIn(w.ZONES[4][3], lines[0])
        self.assertEqual(w.live_chart([], 40), [])


class LiveGaps(unittest.TestCase):
    def _bar_cols(self, lines):
        import re
        bottom = re.sub(r"\[[^\]]*\]", "", lines[-2])[4:]     # strip markup + y label
        return [ch != " " for ch in bottom]

    def test_short_gaps_filled_long_gaps_kept(self):
        s = [(1000 + t, 70) for t in range(0, 120) if t % 3 != 2]          # every 3rd second missing
        cols = self._bar_cols(w.live_chart(s, 204, 4, 185))                 # 200 columns > 120 s
        self.assertTrue(all(cols[:195]), "short gaps should be bridged")
        s2 = [(1000 + t, 70) for t in list(range(0, 40)) + list(range(80, 120))]   # 40 s dropout
        cols2 = self._bar_cols(w.live_chart(s2, 124, 4, 185))
        self.assertFalse(all(cols2), "a long dropout must stay visible")


class WindowLabel(unittest.TestCase):
    def test_dates_and_relative_day(self):
        from datetime import datetime
        ts = lambda *a: int(datetime(*a).timestamp())
        start, end = ts(2026, 11, 6, 23, 7), ts(2026, 11, 7, 5, 55)
        self.assertEqual(w.window_label(start, end, ts(2026, 11, 7, 9, 0)),
                         "Fri Nov 06 23:07 → Sat Nov 07 05:55 · ended this morning")
        self.assertTrue(w.window_label(start, end, ts(2026, 11, 8, 9, 0)).endswith("ended yesterday"))
        self.assertTrue(w.window_label(start, end, ts(2026, 11, 10, 9, 0)).endswith("ended 3 days ago"))
        nap = w.window_label(ts(2026, 11, 7, 13, 0), ts(2026, 11, 7, 14, 30), ts(2026, 11, 7, 18, 0))
        self.assertEqual(nap, "Sat Nov 07 13:00 → 14:30 · ended today")


class HourAxis(unittest.TestCase):
    def test_ticks_align_and_thin_out(self):
        from datetime import datetime
        ts = lambda *a: int(datetime(*a).timestamp())
        start, end = ts(2026, 11, 7, 23, 28), ts(2026, 11, 8, 7, 5)       # 7h37m
        ticks, labels = w.hour_axis(start, end, 120)
        self.assertEqual((len(ticks), len(labels)), (120, 120))
        self.assertEqual(ticks.count("╵"), 8)                               # 00:00 … 07:00
        self.assertTrue(labels.rstrip().endswith("07:00"))                  # edge label right-aligned
        c = ticks.index("╵")
        self.assertEqual(labels[c:c + 5], "00:00")
        self.assertEqual(c, int((ts(2026, 11, 8, 0, 0) - start) / ((end - start) / 120)))
        narrow, _ = w.hour_axis(start, end, 30)                             # 8 labels won't fit in 30
        self.assertLess(narrow.count("╵"), 8)


class HourAxisEnds(unittest.TestCase):
    def test_start_end_labels(self):
        from datetime import datetime
        ts = lambda *a: int(datetime(*a).timestamp())
        start, end = ts(2026, 11, 7, 23, 28), ts(2026, 11, 8, 7, 5)
        ticks, labels = w.hour_axis(start, end, 110, ends=True)
        self.assertTrue(labels.startswith("23:28 "))
        self.assertTrue(labels.endswith(" 07:05"))
        self.assertNotIn("07:00", labels)              # would collide with the end label
        self.assertEqual((ticks[0], ticks[-1]), ("╵", "╵"))
        self.assertEqual(len(labels), 110)
        self.assertNotIn("╵╵", ticks)                  # no hour tick jammed against an end tick
        for width in (30, 60, 110):                    # every hour label sits exactly under a tick
            ticks, labels = w.hour_axis(start, end, width, ends=True)
            import re
            for m in re.finditer(r"\d\d:\d\d", labels):
                self.assertEqual(ticks[m.start()] if m.start() < width - 5 else ticks[-1], "╵", (width, m.group()))


class HRChart(unittest.TestCase):
    def test_peak_keeps_short_workouts_and_gaps_stay(self):
        # 3 h of minutes at rest with a 20-min Z4 burst, then 1 h strap-off, then rest again
        pts = [(t, 160 if 3600 <= t < 4800 else 65) for t in range(0, 3 * 3600, 60)]
        pts += [(t, 65) for t in range(4 * 3600, 5 * 3600, 60)]
        rows = w.zone_chart(pts, 44, 4, 185, t0=0, t1=5 * 3600, agg="max", gap_fill_s=61, rest_hr=0)
        z4 = w.ZONES[1][3]
        self.assertIn(z4, "".join(rows))                       # the burst survives as Z4 at 40 cols
        mean_rows = w.zone_chart(pts, 14, 4, 185, t0=0, t1=5 * 3600, agg="mean", gap_fill_s=61, rest_hr=0)
        self.assertNotIn(w.ZONES[0][3], "".join(mean_rows))    # sanity: never Z5 here
        import re
        bottom = re.sub(r"\[[^\]]*\]", "", rows[-1])[4:]
        self.assertIn(" ", bottom.strip())                      # the strap-off hour stays empty

    def test_time_axis_names_midnights(self):
        from datetime import datetime
        t0 = int(datetime(2026, 11, 5, 9, 0).timestamp())
        ticks, labels = w.time_axis(t0, t0 + 72 * 3600, 120)
        for day in ("Fri", "Sat", "Sun"):
            self.assertIn(day, labels)
        for m in __import__("re").finditer(r"\S+", labels):
            self.assertEqual(ticks[m.start()], "╵")


class Coach(unittest.TestCase):
    """coach.py: the LLM-facing summary (snapshot@3), drill-downs and the daily archive. Synthetic data."""

    @staticmethod
    def ts(*a):
        import coach
        from datetime import datetime
        return int(datetime(*a, tzinfo=coach.TZ).timestamp())

    def week(self, now):
        """7 local days + lead: asleep 00:00-07:00 at 55 bpm, a 40-min run at 150 bpm at 18:00, else 75."""
        import coach
        days = coach.window_days(coach.local_date(now), 7)
        since = coach.day_bounds(days[0])[0] - coach.SLEEP_LEAD_S
        mins, sleep = [], []
        for t in range(since, now, 60):
            h = coach.datetime.fromtimestamp(t, coach.TZ).hour
            m = coach.datetime.fromtimestamp(t, coach.TZ).minute
            asleep = h < 7
            hr = 55 if asleep else 150 if h == 18 and m < 40 else 75
            mins.append(w.Minute(t, float(hr), 0.01 if asleep else 0.2, 60, asleep))
        for d in days:
            a = coach.day_bounds(d)[0]
            if a + 7 * 3600 <= now:
                sleep.append(w.SleepWindow(a, a + 7 * 3600, [m for m in mins if a <= m.t < a + 7 * 3600]))
        meta = {"address": "AA:BB", "name": "test", "newest_unix": now - 60, "labels": [], "sleep_source": "heuristic"}
        r = w.Report(meta, since, now, [w.Sample(now - 60, 75, 0.1, -0.4, 0.91)], mins, sleep)
        return coach.Window(r, now), days

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        p = mock.patch.object(w, "STATE_DIR", self.dir)      # no real sync.json / battery.json
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_workouts_merge_short_dips_and_drop_short_runs(self):
        import coach
        hr = [70] * 5 + [150] * 6 + [100] * 2 + [150] * 6 + [70] * 10 + [150] * 9 + [70] * 5
        hr += [150] * 6 + [100] * 3 + [150] * 6 + [70] * 3       # a 3-min dip splits: two 6-min runs, both too short
        mins = [w.Minute(i * 60, float(b), 0.2, 60) for i, b in enumerate(hr)]
        found = coach.detect_workouts(mins)
        self.assertEqual([(x["start_unix"] // 60, x["duration_min"]) for x in found], [(5, 14)])
        self.assertEqual((found[0]["max_hr"], found[0]["zone_min"]), (150, {"Z2": 12}))

    def test_gaps_and_dst_day(self):
        import coach
        from datetime import date
        mins = [w.Minute(t, 70.0, 0.1, 60) for t in list(range(0, 600, 60)) + list(range(600 + 45 * 60, 6000, 60))]
        self.assertEqual([g["min"] for g in coach.detect_gaps(mins)], [45])
        if coach.TZ_NAME == "America/New_York":
            a, b = coach.day_bounds(date(2026, 11, 1))           # DST ends: a 25-hour day
            self.assertEqual(b - a, 25 * 3600)

    def test_summary_is_small_and_has_no_per_minute_arrays(self):
        import coach, json
        now = self.ts(2026, 11, 11, 20, 0)
        win, days = self.week(now)
        doc = coach.summary(win, days)
        raw = json.dumps(doc, separators=(",", ":"))
        self.assertLess(len(raw), 3584, len(raw))      # soft target (COACH.md): 3.5 KB for 7 full days
        self.assertEqual(doc["schema"], "whoop-poc/snapshot@3")
        self.assertEqual(len(doc["days"]), 7)
        self.assertEqual(len(doc["workouts"]["rows"]), 7)
        self.assertNotIn("per_minute", raw)
        today = doc["days"][-1]
        self.assertTrue(today["open"])
        self.assertEqual((today["sleep"]["asleep_min"], today["zone_min"], today["active_min"]), (420, {"Z2": 40}, 40))
        x = dict(zip(doc["workouts"]["cols"], doc["workouts"]["rows"][-1]))
        self.assertEqual((x["local"], x["duration_min"], x["avg_hr"]), ("11-11 18:00-18:40", 40, 150))
        self.assertEqual(doc["trends"]["rest_hr"]["delta"], 0)
        self.assertEqual(doc["cursor_unix"], now - 60)

    def test_since_keeps_only_newer_records(self):
        import coach
        now = self.ts(2026, 11, 11, 20, 0)
        win, days = self.week(now)
        doc = coach.summary(win, days, since=self.ts(2026, 11, 11, 12, 0))
        self.assertEqual([r["date"] for r in doc["days"]], ["2026-11-11"])
        self.assertEqual(len(doc["workouts"]["rows"]), 1)
        self.assertIn("baseline", doc["trends"]["rest_hr"])      # trends still use the full window

    def test_workout_drill_down(self):
        import coach
        now = self.ts(2026, 11, 11, 20, 0)
        win, _ = self.week(now)
        doc = coach.workout_view(win, self.ts(2026, 11, 11, 18, 10))
        self.assertEqual(doc["schema"], "whoop-poc/workout@2")
        self.assertNotIn("hr_per_minute", doc)
        self.assertEqual(len(coach.workout_view(win, self.ts(2026, 11, 11, 18, 10), series="1m")["hr_per_minute"]), 40)
        self.assertIsNone(coach.workout_view(win, self.ts(2026, 11, 11, 13, 0)))

    def test_archive_writes_final_once_and_prunes(self):
        import coach, json, os
        now = self.ts(2026, 11, 11, 20, 0)
        win, days = self.week(now)
        out = self.dir / "daily"
        out.mkdir()
        (out / "2026-10-01.json").write_text("{}")             # older than 30 days
        (out / "2026-10-20.json").write_text("{}")
        written = coach.write_archive(win, days, out)
        self.assertEqual(len(written), 7)
        self.assertFalse((out / "2026-10-01.json").exists())
        self.assertTrue((out / "2026-10-20.json").exists())
        y = json.loads((out / "2026-11-10.json").read_text())
        self.assertEqual((y["schema"], y["final"]), ("whoop-poc/day@2", True))
        self.assertEqual(len(y["hr_per_minute"]), 1440)
        self.assertEqual(os.stat(out / "2026-11-10.json").st_mode & 0o777, 0o600)
        self.assertTrue(all(t > y["start_unix"] for t, _ in y["hr_per_minute"][1:]))
        # second run: final days untouched, unchanged open day not rewritten; later data rewrites today only
        self.assertEqual(coach.write_archive(win, days, out), [])
        win2, days2 = self.week(now + 600)
        self.assertEqual([p.name for p in coach.write_archive(win2, days2, out)], ["2026-11-11.json"])

    def jog(self, confirmations=None, now=None):
        """Nov 10 and 11, 75 bpm, with the Nov 11 jog at 157 bpm 08:24-08:46 ET (detected 22 min)
        and a 30-min run at 150 bpm on Nov 10 18:00."""
        import coach
        from datetime import date
        now = now or self.ts(2026, 11, 11, 20, 0)
        days = [date(2026, 11, 10), date(2026, 11, 11)]
        since = coach.day_bounds(days[0])[0] - coach.SLEEP_LEAD_S
        jog = (self.ts(2026, 11, 11, 8, 24), self.ts(2026, 11, 11, 8, 46))
        run = (self.ts(2026, 11, 10, 18, 0), self.ts(2026, 11, 10, 18, 30))
        mins = [w.Minute(t, 157.0 if jog[0] <= t < jog[1] else 150.0 if run[0] <= t < run[1] else 75.0, 0.2, 60)
                for t in range(since, now, 60)]
        meta = {"address": "AA:BB", "name": "test", "newest_unix": now - 60, "labels": [], "sleep_source": "heuristic"}
        r = w.Report(meta, since, now, [w.Sample(now - 60, 75, 0.1, -0.4, 0.91)], mins, [])
        return coach.Window(r, now, confirmations), days, jog

    def test_confirmed_duration_overrides_detected(self):
        import coach
        win, days, jog = self.jog()
        self.assertEqual([(x["duration_min"], x["needs_confirm"], x["confirmed"]) for x in win.workouts],
                         [(30, True, False), (22, True, False)])
        # Will: exactly 20 min. Stored against a start 2 min off what detection now says: still matches.
        conf = [{"start_unix": jog[0] + 120, "minutes": 20, "actual_start_unix": jog[0] + 60, "set_unix": 0}]
        win, days, _ = self.jog(conf)
        x = win.workouts[1]
        self.assertEqual((x["duration_min"], x["detected_min"], x["confirmed"], x["needs_confirm"]), (20, 22, True, False))
        self.assertEqual((x["start_unix"], x["confirmed_start_unix"], x["local"]), (jog[0], jog[0] + 60, "11-11 08:24-08:46"))
        self.assertTrue(win.workouts[0]["needs_confirm"])
        doc = coach.summary(win, days)
        rows = [dict(zip(doc["workouts"]["cols"], r)) for r in doc["workouts"]["rows"]]
        self.assertEqual([(r["duration_min"], r["detected_min"], r["needs_confirm"]) for r in rows],
                         [(30, 30, True), (20, 22, False)])
        self.assertEqual(coach.workout_view(win, jog[0])["duration_min"], 20)
        # outside CONFIRM_MATCH_S it doesn't apply
        far = [{**conf[0], "start_unix": jog[0] + coach.CONFIRM_MATCH_S + 60}]
        self.assertTrue(self.jog(far)[0].workouts[1]["needs_confirm"])
        # re-applying is stable (archive files go through it again)
        self.assertEqual(coach.apply_confirmations(win.workouts, conf), win.workouts)
        self.assertEqual(coach.apply_confirmations(win.workouts, [])[1]["duration_min"], 22)

    def test_since_returns_workouts_confirmed_after_it(self):
        import coach
        since = self.ts(2026, 11, 11, 12, 0)
        win, days, _ = self.jog()
        self.assertEqual(coach.summary(win, days, since=since)["workouts"]["rows"], [])
        conf = [{"start_unix": self.ts(2026, 11, 11, 8, 24), "minutes": 20, "set_unix": since + 60}]
        win, days, _ = self.jog(conf)
        rows = coach.summary(win, days, since=since)["workouts"]["rows"]
        self.assertEqual([r[3] for r in rows], [20])

    def test_final_archive_file_picks_up_later_confirmation(self):
        import coach, json
        win, days, jog = self.jog()
        out = self.dir / "daily"
        coach.write_archive(win, days, out, today=days[-1])
        p = out / "2026-11-10.json"
        self.assertTrue(json.loads(p.read_text())["final"])
        conf = [{"start_unix": self.ts(2026, 11, 10, 18, 0), "minutes": 35, "set_unix": 0}]
        win, days, _ = self.jog(conf)
        self.assertIn(p, coach.write_archive(win, days, out, today=days[-1]))
        y = json.loads(p.read_text())
        self.assertEqual((y["final"], y["workouts"][0]["duration_min"], y["workouts"][0]["detected_min"]), (True, 35, 30))
        self.assertEqual(coach.write_archive(win, days, out, today=days[-1]), [])     # unchanged: not rewritten

    def test_cli_confirm_and_clear(self):
        import coach, io, json
        from contextlib import redirect_stdout
        from unittest import mock
        win, days, jog = self.jog()
        db = self.dir / "whoop.db"
        db.write_text("")
        coach.write_archive(win, days, self.dir / "daily", today=days[-1])
        w.write_json(self.dir / "coach.json", coach.summary(win, days), compact=True)

        def cli(*args):
            buf = io.StringIO()
            with mock.patch.object(coach, "load", lambda *a, **k: self.jog()[0]), redirect_stdout(buf), \
                    self.assertRaises(SystemExit) as cm:
                w.main(["coach", "confirm", *args, "--db", str(db)])
            return json.loads(buf.getvalue()), cm.exception.code

        doc, code = cli("2026-11-11T08:24", "--minutes", "20", "--start", "08:25")
        self.assertEqual(code, 0, doc)
        self.assertEqual((doc["workout"]["duration_min"], doc["workout"]["detected_min"]), (20, 22))
        self.assertEqual(doc["workout"]["confirmed_start_unix"], jog[0] + 60)
        stored = json.loads((self.dir / "workout-confirm.json").read_text())
        self.assertEqual(stored["schema"], "whoop-poc/workout-confirm@1")
        self.assertEqual([(c["start_unix"], c["minutes"]) for c in stored["workouts"]], [(jog[0], 20)])
        summ = json.loads((self.dir / "coach.json").read_text())["workouts"]
        self.assertEqual(dict(zip(summ["cols"], summ["rows"][-1]))["duration_min"], 20)
        day = json.loads((self.dir / "daily" / "2026-11-11.json").read_text())
        self.assertEqual(day["workouts"][0]["duration_min"], 20)
        # confirming again replaces, it doesn't add
        self.assertEqual(cli("2026-11-11T08:26", "--minutes", "20")[1], 0)
        self.assertEqual(len(json.loads((self.dir / "workout-confirm.json").read_text())["workouts"]), 1)
        self.assertEqual(cli("2026-11-11T08:24", "--clear")[1], 0)
        self.assertEqual(json.loads((self.dir / "workout-confirm.json").read_text())["workouts"], [])
        self.assertEqual(json.loads((self.dir / "daily" / "2026-11-11.json").read_text())["workouts"][0]["duration_min"], 22)
        self.assertEqual(cli("2026-11-11T08:24", "--clear")[1], 1)          # nothing left to clear
        self.assertEqual(cli("2026-11-11T13:00", "--minutes", "20")[1], 1)  # no workout there
        self.assertEqual(cli("2026-11-11T08:24")[1], 2)                     # neither --minutes nor --clear

    def test_active_min_counts_confirmed_workouts_and_loose_zone_minutes(self):
        import coach
        from datetime import date
        conf = [{"start_unix": self.ts(2026, 11, 11, 8, 24), "minutes": 20, "set_unix": 0}]
        win, days, _ = self.jog(conf)
        self.assertEqual(win.active_min(date(2026, 11, 11)), 20)      # confirmed 20 wins over detected 22
        self.assertEqual(win.active_min(date(2026, 11, 10)), 30)
        self.assertIsNone(win.active_min(date(2026, 11, 8)))          # no worn minute: no data, not 0
        loose = self.ts(2026, 11, 11, 12, 0)                          # one Z1+ minute outside any workout
        next(m for m in win.mins if m.t == loose).hr = 130.0
        doc = coach.summary(win, days)
        self.assertEqual([r["active_min"] for r in doc["days"]], [30, 21])
        t = doc["trends"]["active_min"]
        self.assertEqual((t["latest"], t["n"], t["baseline"], t["delta"]), (21, 1, None, None))   # open day is the latest
        self.assertIn("has 1", t["why"])
        t = coach.summary(*self.week(self.ts(2026, 11, 11, 20, 0)))["trends"]["active_min"]
        self.assertEqual((t["latest"], t["baseline"], t["n"]), (40, 40, 6))

    def test_short_or_daytime_sleep_is_a_nap_and_baseline_needs_three_nights(self):
        import coach
        from datetime import date
        nap = w.SleepWindow(self.ts(2026, 11, 10, 19, 43), self.ts(2026, 11, 10, 20, 48))
        day = w.SleepWindow(self.ts(2026, 11, 10, 11, 0), self.ts(2026, 11, 10, 15, 0))
        night = w.SleepWindow(self.ts(2026, 11, 10, 23, 59), self.ts(2026, 11, 11, 8, 16))
        self.assertEqual([coach.is_night(s) for s in (nap, day, night)], [False, False, True])
        win, days, _ = self.jog()
        for s in (nap, night):
            s.minutes = [m for m in win.mins if s.start <= m.t < s.end]
            for m in s.minutes:
                m.asleep = True
        win.r.sleep[:] = [nap, night]
        rows = [win.day_row(d) for d in days]
        self.assertNotIn("sleep", rows[0])
        self.assertEqual(rows[0]["nap_min"], nap.asleep_min)
        self.assertEqual(rows[1]["sleep"]["local"], "23:59-08:16")
        t = coach.summary(win, days)["trends"]["sleep_asleep_min"]
        self.assertEqual((t["n"], t["baseline"], t["delta"]), (0, None, None))
        self.assertIn("nights", t["why"])
        self.assertEqual([s["night"] for s in coach.day_view(win, date(2026, 11, 10))["sleep"]], [False])
        t = coach.summary(*self.week(self.ts(2026, 11, 11, 20, 0)))["trends"]["sleep_asleep_min"]
        self.assertEqual((t["n"], t["baseline"], t["delta"]), (6, 420, 0))
        self.assertNotIn("why", t)

    def test_days_without_data_are_no_data_not_worn_zero(self):
        import coach
        from datetime import date
        win, _, _ = self.jog()
        days = [date(2026, 11, 7), date(2026, 11, 8), date(2026, 11, 10)]
        rows = coach.summary(win, days)["days"]
        self.assertEqual(rows[0], {"date": "2026-11-07", "start_unix": coach.day_bounds(days[0])[0], "no_data": True})
        self.assertNotIn("worn_min", rows[1])
        self.assertEqual(rows[2]["worn_min"], 1440)
        out = self.dir / "daily"
        coach.write_archive(win, days, out, today=date(2026, 11, 11))
        self.assertEqual(sorted(p.name for p in out.glob("*.json")), ["2026-11-10.json"])

    def test_freshness_hint_when_not_advertising_and_stale(self):
        import coach, socket
        now = self.ts(2026, 11, 11, 20, 0)
        win, _, _ = self.jog(now=now)
        win.newest = now - 3 * 3600 - 7 * 60
        w.write_json(self.dir / "sync.json", {"outcome": "not_advertising", "finished_unix": now})
        f = coach.freshness(win)
        self.assertEqual(f["hint"], f"strap out of range of {socket.gethostname()} (not advertising); "
                                    "no new data for 3 h 7 min")
        win.newest = now - 20 * 60                                     # not stale: no hint
        self.assertNotIn("hint", coach.freshness(win))
        win.newest = now - 3 * 3600
        w.write_json(self.dir / "sync.json", {"outcome": "complete", "finished_unix": now})
        self.assertNotIn("hint", coach.freshness(win))

    def test_cli_json_out_and_raw(self):
        import io, json
        from contextlib import redirect_stdout
        out = self.dir / "coach.json"
        w.main(["json", "--demo", "--heuristic", "--out", str(out)])
        doc = json.loads(out.read_text())
        self.assertEqual(doc["schema"], "whoop-poc/snapshot@3")
        self.assertTrue(doc["demo"])
        self.assertFalse((self.dir / "daily").exists())        # demo data is never archived
        buf = io.StringIO()
        with redirect_stdout(buf):
            w.main(["json", "--raw", "--demo", "--heuristic"])
        self.assertEqual(json.loads(buf.getvalue())["schema"], "whoop-poc/snapshot@1")
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            w.main(["coach", "bogus", "--demo", "--heuristic"])
        self.assertEqual(cm.exception.code, 2)

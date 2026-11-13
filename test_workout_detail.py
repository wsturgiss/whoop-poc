"""workout_detail.py and `coach workout` (whoop-poc/workout@2). Synthetic data only."""
import io
import json
import math
import sqlite3
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

import coach
import whoop_poc as w
import workout_detail as wd

T0 = int(datetime(2026, 11, 11, 8, 24, tzinfo=coach.TZ).timestamp())


def hr_trace(*legs, t0=T0):
    """{unix: bpm} from legs (seconds, bpm) or (seconds, bpm_from, bpm_to) for a linear ramp."""
    out, t = {}, t0
    for leg in legs:
        n, a = leg[0], leg[1]
        b = leg[2] if len(leg) > 2 else a
        for i in range(n):
            out[t + i] = round(a + (b - a) * i / max(n - 1, 1))
        t += n
    return out


def samples(hr: dict[int, int], moving=None) -> list[w.Sample]:
    """Per-second samples; `moving(t)` true flips the gravity vector each second (wrist motion)."""
    out = []
    for t in sorted(hr):
        flip = moving is not None and moving(t) and t % 2
        out.append(w.Sample(t, hr[t], 0.6 if flip else 0.1, -0.4, 0.7 if flip else 0.91))
    return out


def window(hr: dict[int, int], moving=None, confirmations=()) -> coach.Window:
    ss = samples(hr, moving)
    mins = w.per_minute(ss)
    now = ss[-1].unix + 60
    meta = {"address": "AA:BB", "name": "test", "newest_unix": now, "labels": [], "sleep_source": "heuristic"}
    return coach.Window(w.Report(meta, ss[0].unix, now, ss, mins, []), now, list(confirmations))


class Peak(unittest.TestCase):
    def test_5s_peak_smooths_a_one_second_spike(self):
        hr = hr_trace((60, 150))
        hr[T0 + 30] = 200
        self.assertEqual(wd.peak_5s(hr), 160)                  # (4 × 150 + 200) / 5
        hr.update({T0 + 40 + i: 180 for i in range(5)})
        self.assertEqual(wd.peak_5s(hr), 180)

    def test_5s_peak_skips_windows_with_dropouts(self):
        hr = hr_trace((60, 150))
        for t in (T0 + 27, T0 + 28, T0 + 29, T0 + 31, T0 + 32, T0 + 33):
            del hr[t]
        hr[T0 + 30] = 200                                        # alone in its window: not a 5 s peak
        self.assertEqual(wd.peak_5s(hr), 150)
        self.assertIsNone(wd.peak_5s({}))


class Recovery(unittest.TestCase):
    def trace(self):
        # 20 min at 160, then falling 0.5 bpm/s to 100
        return hr_trace((1200, 160), (121, 160, 100))

    def test_effort_end_is_before_the_detected_end(self):
        hr = hr_trace((1200, 160), (300, 100))
        self.assertEqual(wd.effort_end(hr, T0, T0 + 1260, 160), T0 + 1200)

    def test_drops_at_one_and_two_minutes(self):
        r = wd.recovery(self.trace(), T0 + 1200)
        self.assertEqual((r["end_hr"], r["drop_1min"], r["drop_2min"]), (160, 30, 60))
        self.assertNotIn("why", r)

    def test_strap_gap_makes_a_drop_null_with_why(self):
        hr = self.trace()
        for t in range(T0 + 1250, T0 + 1270):
            hr.pop(t, None)
        r = wd.recovery(hr, T0 + 1200)
        self.assertIsNone(r["drop_1min"])
        self.assertEqual(r["drop_2min"], 60)
        self.assertIn("+1 min (strap gap)", r["why"])
        self.assertIsNone(wd.recovery(hr, None)["drop_1min"])


class Drift(unittest.TestCase):
    def test_steady_effort_reports_second_minus_first_half(self):
        hr = hr_trace((1800, 150, 160))                           # steady, drifting up 10 bpm over 30 min
        d = wd.drift(hr, T0, T0 + 1800, False)
        self.assertTrue(d["steady"])
        # core = 5..30 min: halves centred at 11.25 and 23.75 min → 153.75 and 157.9
        self.assertAlmostEqual(d["drift_bpm"], 4.2, delta=0.2)
        self.assertEqual((d["first_half_hr"], d["second_half_hr"]), (154, 158))

    def test_surges_short_efforts_and_intervals_are_not_steady(self):
        hr = hr_trace((900, 150), (240, 180), (660, 150))
        d = wd.drift(hr, T0, T0 + 1800, False)
        self.assertEqual((d["steady"], d["drift_bpm"]), (False, None))
        self.assertIn("RMS from a line", d["why"])
        self.assertIn("under 10 min", wd.drift(hr_trace((600, 150)), T0, T0 + 600, False)["why"])
        self.assertEqual(wd.drift(hr, T0, T0 + 1800, True)["why"], "intervals detected")


class ZoneTime(unittest.TestCase):
    def test_z4_plus_counts_seconds_at_or_above_z4(self):
        lo = w.zone_low(w.ZONES[1])
        self.assertEqual(lo, 164)
        hr = hr_trace((600, 150), (120, lo), (30, 180), (300, 150))
        self.assertEqual(wd.z4_plus_min(hr), 2.5)


class Intervals(unittest.TestCase):
    def test_four_by_three_minutes(self):
        hr = hr_trace((120, 130), *[leg for _ in range(4) for leg in ((180, 175), (120, 130))])
        segs = wd.intervals(hr, T0, max(hr) + 1)
        kinds = [g["kind"] for g in segs]
        self.assertEqual(kinds.count("work"), 4)
        self.assertEqual(kinds[:2], ["work", "rest"])
        work = [g for g in segs if g["kind"] == "work"]
        self.assertTrue(all(g["peak_hr_5s"] == 175 and 150 <= g["sec"] <= 210 for g in work), work)
        rests = [g for g in segs if g["kind"] == "rest"]
        self.assertTrue(all(g["hr_drop"] == 45 for g in rests), rests)

    def test_steady_and_a_single_slow_down_give_none(self):
        self.assertEqual(wd.intervals(hr_trace((1800, 155)), T0, T0 + 1800), [])
        # one surge, then the final descent (cool-down): not intervals
        hr = hr_trace((300, 140), (120, 180), (600, 155), (120, 175), (60, 140))
        self.assertEqual(wd.intervals(hr, T0, max(hr) + 1), [])


class Lifting(unittest.TestCase):
    def test_sets_from_wrist_motion(self):
        hr = hr_trace((60, 110), *[leg for _ in range(3) for leg in ((40, 130), (90, 115))])
        sets = [(T0 + 60 + k * 130, T0 + 100 + k * 130) for k in range(3)]
        r = wd.lift_sets(samples(hr, lambda t: any(a <= t < b for a, b in sets)), T0, max(hr) + 1)
        self.assertEqual(r["sets"], 3)
        self.assertTrue(all(abs(s - 40) <= 6 for s in r["set_sec"]), r)
        self.assertTrue(all(abs(s - 90) <= 6 for s in r["rest_sec"]), r)
        self.assertAlmostEqual(r["total_min"], 5.0, delta=0.2)

    def test_too_little_gravity_is_null_with_why(self):
        r = wd.lift_sets(samples(hr_trace((60, 110))), T0, T0 + 600)
        self.assertIsNone(r["sets"])
        self.assertIn("under half", r["why"])


def imu_frame(unix, mags_fn, kind="imu"):
    """A type-43 frame: IMU (payload 1917) or optical (1921) layout, accel on Z only."""
    data_len = wd.IMU_DATA_LEN if kind == "imu" else 1921
    b = bytearray(data_len + 11)
    b[0], b[4] = 0xAA, wd.IMU_TYPE
    struct.pack_into("<H", b, 1, data_len + 7)
    struct.pack_into("<I", b, 11, unix)
    if kind == "imu":
        struct.pack_into("<100h", b, wd.IMU_AXES[2], *[round(mags_fn(i) * 4096) for i in range(100)])
    return bytes(b)


class Cadence(unittest.TestCase):
    def steps(self, spm, seconds, t0=T0):
        f = spm / 60
        return [(t0 + s, [1 + 0.5 * math.sin(2 * math.pi * f * (s + i / 100)) for i in range(100)])
                for s in range(seconds)]

    def test_step_rate_from_imu(self):
        r = wd.cadence(self.steps(168, 120))
        self.assertAlmostEqual(r["cadence_spm"], 168, delta=3)
        self.assertEqual(r["imu_sec"], 120)

    def test_no_imu_is_null_with_why(self):
        r = wd.cadence([])
        self.assertIsNone(r["cadence_spm"])
        self.assertIn("live session", r["why"])
        still = [(T0 + s, [1.0] * 100) for s in range(120)]
        self.assertIn("no periodic", wd.cadence(still)["why"])

    def test_load_imu_decodes_type_43(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "whoop.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE frames (device_id, recv_ms, inner_type, unix, hex)")
            rows = [(1, (T0 + s + 1) * 1000, 43, None, imu_frame(T0 + s, lambda i: 1.0).hex()) for s in range(5)]
            rows.append((1, (T0 + 2) * 1000, 43, None, imu_frame(T0 + 1, lambda i: 1.0, "optical").hex()))
            rows.append((1, (T0 + 99) * 1000, 43, None, imu_frame(T0 - 500, lambda i: 1.0).hex()))   # before a
            conn.executemany("INSERT INTO frames VALUES (?,?,?,?,?)", rows)
            conn.commit()
            conn.close()
            got = wd.load_imu(db, T0, T0 + 60)
        self.assertEqual([t for t, _ in got], [T0 + s for s in range(5)])
        self.assertTrue(all(abs(v - 1.0) < 1e-3 for _, m in got for v in m))
        self.assertEqual(wd.load_imu(Path("/nonexistent/whoop.db"), T0, T0 + 60), [])


class WorkoutView(unittest.TestCase):
    """`coach workout <id>`: the detail document, IDs, kinds and the optional HR series."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(w, "STATE_DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        # 5 min at 80, a 20-min run at 160 from 08:29, then recovery to 90 and 10 min at 80
        self.hr = hr_trace((300, 80), (1200, 160), (61, 160, 100), (60, 100, 90), (600, 80), t0=T0 - 300)
        self.win = window(self.hr)

    def test_id_lookup_and_fields(self):
        x = self.win.workouts[0]
        self.assertEqual(x["id"], "2026-11-11-0824")
        self.assertEqual(coach.parse_time("2026-11-11-0824", T0), T0)
        doc = coach.workout_view(self.win, coach.parse_time(x["id"], T0))
        self.assertEqual((doc["schema"], doc["id"]), ("whoop-poc/workout@2", "2026-11-11-0824"))
        for k in ("duration_min", "detected_min", "needs_confirm", "confirmed", "peak_hr_5s", "peak_hr_1s",
                  "z4_plus_min", "recovery", "drift", "intervals", "cadence"):
            self.assertIn(k, doc)
        self.assertEqual((doc["peak_hr_5s"], doc["peak_hr_1s"], doc["intervals"]), (160, 160, []))
        self.assertTrue(doc["drift"]["steady"])
        self.assertEqual(doc["recovery"]["drop_1min"], 59)          # 160 → ~101 one minute later
        self.assertNotIn("hr_per_minute", doc)                  # no arrays by default
        self.assertEqual(coach.workout_view(self.win, x["start_unix"])["id"], x["id"])   # unix still works

    def test_confirmed_duration_carries_into_the_detail(self):
        start = self.win.workouts[0]["start_unix"]
        win = window(self.hr, confirmations=[{"start_unix": start, "minutes": 18, "set_unix": 0}])
        doc = coach.workout_view(win, start)
        self.assertEqual((doc["duration_min"], doc["needs_confirm"], doc["confirmed"]), (18, False, True))

    def test_lift_kind_and_series(self):
        doc = coach.workout_view(self.win, T0, kind="lift")
        self.assertIn("lifting", doc)
        for k in ("recovery", "drift", "intervals", "cadence", "z4_plus_min"):
            self.assertNotIn(k, doc)
        self.assertEqual(len(coach.workout_view(self.win, T0, series="1m")["hr_per_minute"]),
                         self.win.workouts[0]["detected_min"])
        s10 = coach.workout_view(self.win, T0, series="10s")["hr_10s"]
        self.assertEqual(s10[1][0] - s10[0][0], 10)

    def test_summary_lists_the_id(self):
        doc = coach.summary(self.win, [coach.local_date(T0)])
        self.assertEqual(doc["workouts"]["cols"][-1], "id")
        self.assertEqual(doc["workouts"]["rows"][0][-1], "2026-11-11-0824")

    def test_cli(self):
        db = Path(self.tmp.name) / "whoop.db"
        db.write_text("")

        def cli(*args):
            buf = io.StringIO()
            with mock.patch.object(coach, "load", lambda *a, **k: self.win), redirect_stdout(buf), \
                    self.assertRaises(SystemExit) as cm:
                w.main(["coach", "workout", *args, "--db", str(db)])
            return json.loads(buf.getvalue()), cm.exception.code

        doc, code = cli("2026-11-11-0824")
        self.assertEqual((code, doc["id"], doc["cadence"]["cadence_spm"]), (0, "2026-11-11-0824", None))
        self.assertEqual(cli("2026-11-11-0824", "--kind", "swim")[1], 2)
        self.assertEqual(cli("2026-11-11-0824", "--series", "1s")[1], 2)
        self.assertEqual(cli("2026-11-11-1300")[1], 1)


if __name__ == "__main__":
    unittest.main()


class TuiWorkouts(unittest.TestCase):
    """coach.workout_rows (the TUI's workout fields) and the TUI's text for them."""

    def setUp(self):
        self.hr = hr_trace((300, 80), (1200, 160), (61, 160, 100), (60, 100, 90), (600, 80), t0=T0 - 300)

    def test_rows_carry_workout_view_fields(self):
        (x,) = coach.workout_rows(window(self.hr))
        self.assertEqual(set(x), set(coach.TUI_WORKOUT_KEYS))
        self.assertEqual((x["id"], x["confirmed"], x["recovery"]["drop_1min"]), ("2026-11-11-0824", False, 59))

    def test_report_with_workouts_drops_samples(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(w, "STATE_DIR", Path(d)):
            r = coach.report_with_workouts(None, 24, None, now=1793880000, demo=True, heuristic=True)
        self.assertEqual(r.meta["workouts"], [])            # the demo night has no workout
        self.assertLessEqual(len(r.samples), 1)

    def test_tui_text_marks_unconfirmed_and_uses_confirmed_minutes(self):
        import tui
        (x,) = coach.workout_rows(window(self.hr))
        self.assertEqual(tui.workout_status(x), "likely workout")
        self.assertIn("likely workout (elevated HR, not confirmed)", tui.workout_lines(x))
        self.assertIn("HR −59 bpm after 1 min", tui.workout_lines(x))
        start = x["start_unix"]
        (c,) = coach.workout_rows(window(self.hr, confirmations=[{"start_unix": start, "minutes": 18, "set_unix": 0}]))
        self.assertEqual((tui.workout_status(c), c["duration_min"]), ("confirmed", 18))
        self.assertIn(f"0h 18m (confirmed; detected {w.dur(c['detected_min'])})", tui.workout_lines(c))
        self.assertEqual(tui.zones_text({"Z3": 14, "Z4": 4}), "Z3 14 · Z4 4")
        self.assertEqual(tui.recovery_text({"drop_1min": None}), "—")

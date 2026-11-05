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
        r = w.build_report(None, 24, None, now=self.END, demo=True)
        self.assertIn("asleep", "\n".join(t for _, t in w.text_lines(r, 100)))
        self.assertIn('<rect class="band"', w.render_html(r))


if __name__ == "__main__":
    unittest.main()

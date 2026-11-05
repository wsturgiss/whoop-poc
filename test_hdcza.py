"""HDCZA in-bed window + in-bed bands on synthetic gravity (no real biometrics)."""
import math
import random
import unittest

import hdcza

T0 = 1_600_000_000


def synth(active_min=60, still_min=300, after_min=60, seed=1):
    """Wrist that moves (angle wanders), lies still with tiny noise, then moves again. 1 Hz."""
    rnd = random.Random(seed)
    out, ang = [], 30.0
    for i in range((active_min + still_min + after_min) * 60):
        still = active_min * 60 <= i < (active_min + still_min) * 60
        ang += rnd.gauss(0, 0.002 if still else 3.0)
        ang = max(-80.0, min(80.0, ang))
        a = math.radians(ang)
        out.append((T0 + i, math.cos(a), 0.0, math.sin(a)))
    return out


class HDCZA(unittest.TestCase):
    def test_window_matches_still_block(self):
        w = hdcza.detect(synth())
        self.assertIsNotNone(w)
        self.assertLess(abs(w.start - (T0 + 3600)), 5 * 60)
        self.assertLess(abs(w.end - (T0 + 3600 + 300 * 60)), 5 * 60)
        self.assertGreaterEqual(w.threshold, hdcza.TMIN)

    def test_short_still_block_ignored(self):
        self.assertIsNone(hdcza.detect(synth(active_min=60, still_min=20, after_min=60)))

    def test_long_gap_is_not_in_bed(self):
        g = synth()
        g = [p for p in g if not (T0 + 2 * 3600 <= p[0] < T0 + 3 * 3600)]   # 1 h strap-off mid-block
        w = hdcza.detect(g)
        self.assertTrue(w.end <= T0 + 2 * 3600 + 300 or w.start >= T0 + 3 * 3600 - 300)

    def test_rolling_median(self):
        self.assertEqual(hdcza.rolling_median_absdiff([0, 1, 3, 6, 10], 3), [0.0, 1.5, 2.5, 3.5, 0.0])


class Bands(unittest.TestCase):
    def test_inbed_wake_around_sleep(self):
        w = hdcza.Window(start=1000, end=10000, threshold=0.13, epochs=0)
        sess = {"start": 2000, "end": 9000, "stages": [
            {"start": 2000, "end": 2600, "stage": "wake"}, {"start": 2600, "end": 5000, "stage": "light"},
            {"start": 5000, "end": 5300, "stage": "wake"}, {"start": 5300, "end": 8000, "stage": "deep"},
            {"start": 8000, "end": 9000, "stage": "wake"}]}
        b = hdcza.in_bed_bands(w, sess)
        self.assertEqual((b["tib_start"], b["tib_end"], b["onset"], b["final_wake"]), (1000, 10000, 2600, 8000))
        self.assertEqual(b["bands"][0], {"start": 1000, "end": 2600, "stage": "inbed_wake"})
        self.assertEqual(b["bands"][-1], {"start": 8000, "end": 10000, "stage": "inbed_wake"})
        self.assertEqual((b["sol_min"], b["lie_in_min"], b["waso_min"]), (26.7, 33.3, 5.0))
        self.assertAlmostEqual(b["se"], (2400 + 2700) / 9000)

    def test_session_only(self):
        b = hdcza.in_bed_bands(None, {"start": 0, "end": 600, "stages": [{"start": 0, "end": 600, "stage": "light"}]})
        self.assertEqual((b["sol_min"], b["lie_in_min"], b["se"]), (0.0, 0.0, 1.0))


if __name__ == "__main__":
    unittest.main()

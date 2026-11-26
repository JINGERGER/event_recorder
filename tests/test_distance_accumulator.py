from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/event_recorder/examples'))
from distance_accumulator import DistanceAccumulator


class DistanceTests(unittest.TestCase):
    def observe(self, tracker, x, y=0.0, stamp=1, frame='odom', child='base_link'):
        return tracker.observe(x, y, frame, child, stamp)

    def test_cumulative_not_displacement_and_strict_threshold(self):
        tracker = DistanceAccumulator(20)
        self.assertIsNone(self.observe(tracker, 100, stamp=1))
        self.assertIsNone(self.observe(tracker, 110, stamp=2))
        self.assertIsNone(self.observe(tracker, 100, stamp=3))  # Exactly 20 m, back at origin.
        event = self.observe(tracker, 101, stamp=4)
        self.assertEqual(event['distance_m'], 21)
        self.assertEqual(event['origin_xy'], [100, 0])
        self.assertIsNone(self.observe(tracker, 130, stamp=5))  # One-shot, not every frame.

    def test_planar_length_reset_and_invalid_samples(self):
        tracker = DistanceAccumulator(4)
        self.observe(tracker, 0, stamp=1)
        self.assertIsNone(self.observe(tracker, float('nan'), stamp=2))
        self.assertEqual(tracker.invalid_samples, 1)
        event = self.observe(tracker, 3, 4, stamp=3)
        self.assertEqual(event['distance_m'], 5)
        tracker.reset()
        self.assertIsNone(self.observe(tracker, 1000, stamp=4))
        self.assertEqual(self.observe(tracker, 1005, stamp=5)['distance_m'], 5)

    def test_duplicate_stamp_and_time_rewind(self):
        tracker = DistanceAccumulator(20)
        self.observe(tracker, 0, stamp=5)
        self.assertIsNone(self.observe(tracker, 50, stamp=5))
        self.assertEqual(tracker.duplicate_samples, 1)
        self.assertEqual(tracker.distance_m, 0)
        self.assertIsNone(self.observe(tracker, 50, stamp=4))
        self.assertIn('backwards', tracker.fault)
        self.assertIsNone(self.observe(tracker, 100, stamp=6))
        tracker.reset()
        self.assertIsNone(self.observe(tracker, 100, stamp=1))

    def test_frame_change_requires_reset(self):
        tracker = DistanceAccumulator(20)
        self.observe(tracker, 0)
        self.assertIsNone(self.observe(tracker, 100, stamp=2, frame='map'))
        self.assertIn('frame_changed', tracker.fault)
        self.assertFalse(tracker.crossed)

    def test_invalid_threshold(self):
        for threshold in [0, -1, float('nan'), float('inf')]:
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                DistanceAccumulator(threshold)


if __name__ == '__main__':
    unittest.main()

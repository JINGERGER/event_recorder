"""Planar odometry path length; independent of ROS for deterministic checks."""
import math


class DistanceAccumulator:
    def __init__(self, threshold_m=20.0):
        if not math.isfinite(threshold_m) or threshold_m <= 0:
            raise ValueError('threshold_m must be finite and positive')
        self.threshold_m = float(threshold_m)
        self.reset()

    def reset(self):
        self.origin = self.previous = None
        self.frame = None
        self.last_stamp_ns = None
        self.distance_m = 0.0
        self.samples = self.invalid_samples = self.duplicate_samples = 0
        self.crossed = False
        self.fault = ''

    def observe(self, x, y, frame_id, child_frame_id, stamp_ns):
        """Return details once, on the first sample with cumulative distance > threshold."""
        if self.fault:
            return None
        if not math.isfinite(x) or not math.isfinite(y) or not frame_id:
            self.invalid_samples += 1
            return None
        frame = (frame_id, child_frame_id)
        if self.frame is not None and frame != self.frame:
            self.fault = 'odometry_frame_changed; explicit reset required'
            return None
        if self.last_stamp_ns is not None and self.last_stamp_ns > 0:
            if stamp_ns < self.last_stamp_ns:
                self.fault = 'odometry_time_went_backwards; explicit reset required'
                return None
            if stamp_ns == self.last_stamp_ns:
                self.duplicate_samples += 1
                return None
        xy = (x, y)
        if self.previous is None:
            self.origin = xy
        else:
            self.distance_m += math.hypot(x - self.previous[0], y - self.previous[1])
        self.previous = xy
        self.frame = frame
        self.last_stamp_ns = stamp_ns
        self.samples += 1
        if not self.crossed and self.distance_m > self.threshold_m:
            self.crossed = True
            return {
                'condition': 'cumulative_odom_xy_distance',
                'threshold_m': self.threshold_m, 'distance_m': self.distance_m,
                'origin_xy': list(self.origin), 'position_xy': list(xy),
                'frame_id': frame_id, 'child_frame_id': child_frame_id,
                'source_stamp_ns': stamp_ns, 'samples': self.samples,
            }
        return None

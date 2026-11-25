#!/usr/bin/env python3
"""Real DDS + rosbag2 integration validation; run after sourcing install/setup.bash."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import rclpy
from rclpy.serialization import deserialize_message
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from std_msgs.msg import String
from sensor_msgs.msg import Imu
from rosgraph_msgs.msg import Clock
from tf2_msgs.msg import TFMessage
from geometry_msgs.msg import TransformStamped
import rosbag2_py
import yaml

ROOT = Path(__file__).resolve().parents[1]


def bag_read(path):
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(path), storage_id=yaml.safe_load((Path(path) / 'metadata.yaml').read_text())['rosbag2_bagfile_information']['storage_identifier']),
                rosbag2_py.ConverterOptions('', ''))
    rows = []
    while reader.has_next():
        rows.append(reader.read_next())
    return rows


class Harness:
    def __init__(self, output, storage='sqlite3'):
        self.storage = storage
        self.output = output
        self.node = rclpy.create_node('event_recorder_validation')
        self.pub = self.node.create_publisher(String, '/test/sequence', 1000)
        self.trigger = self.node.create_publisher(String, '/event_recorder/trigger', 100)
        qos = QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.tf = self.node.create_publisher(TFMessage, '/tf_static', qos)
        self.seq = 0
        self.results = []

    def wait(self, seconds, publish=False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if publish:
                self.pub.publish(String(data=str(self.seq)))
                self.seq += 1
            rclpy.spin_once(self.node, timeout_sec=0)
            time.sleep(0.005)

    def start(self, name, pre=0.5, post=0.6, extra=None, topics=None, types=None):
        self.case_dir = self.output / name
        self.case_dir.mkdir(parents=True)
        params = dict(pre_trigger_seconds=pre, post_trigger_seconds=post,
                      history_retention_seconds=max(pre + 0.5, 1.5),
                      max_event_seconds=60.0, output_directory=str(self.case_dir),
                      storage_id=self.storage, minimum_free_bytes=1,
                      topics=topics or ['/test/sequence'], types=types or ['std_msgs/msg/String'],
                      context_topics=['/tf_static'], context_types=['tf2_msgs/msg/TFMessage'])
        params.update(extra or {})
        config = self.case_dir / 'params.yaml'
        config.write_text(yaml.safe_dump({'event_recorder': {'ros__parameters': params}}))
        self.log = open(self.case_dir / 'recorder.log', 'w')
        self.proc = subprocess.Popen([str(ROOT / 'install/event_recorder/lib/event_recorder/recorder'),
                                      '--ros-args', '--params-file', str(config)], stdout=self.log, stderr=self.log)
        end = time.monotonic() + 10
        while self.trigger.get_subscription_count() == 0:
            assert self.proc.poll() is None, 'recorder exited; inspect recorder.log'
            assert time.monotonic() < end, 'DDS discovery timeout'
            self.wait(0.02)
        self.wait(0.08)

    def fire(self, value):
        self.trigger.publish(String(data=value))
        self.wait(0.03, True)

    def finish(self, expected=1):
        end = time.monotonic() + 10
        while True:
            metas = [yaml.safe_load(p.read_text()) for p in self.case_dir.glob('*.event.yaml')]
            if len(metas) == expected and all(m.get('status') in ('COMPLETED', 'PARTIAL', 'FAILED') for m in metas):
                break
            assert self.proc.poll() is None, 'recorder exited unexpectedly'
            assert time.monotonic() < end, f'finalization timeout: {metas}'
            self.wait(0.05)
        self.stop()
        return sorted(metas, key=lambda m: m['window_start_steady_ns'])

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            self.proc.wait(timeout=15)
        self.log.close()
        assert self.proc.returncode == 0, f'recorder return code {self.proc.returncode}'
        self.wait(0.15)

    def check(self, meta, status='COMPLETED', count_triggers=1, contiguous=True):
        assert meta['status'] == status, meta
        assert len(meta['triggers']) == count_triggers, meta
        bag = self.case_dir / meta['event_id']
        rows = bag_read(bag)
        seq = [int(deserialize_message(data, String).data) for topic, data, _ in rows if topic == '/test/sequence']
        assert len(seq) == len(set(seq)), 'duplicate sequence IDs'
        if contiguous and seq:
            assert seq == list(range(seq[0], seq[-1] + 1)), 'missing/reordered sequence IDs'
        for topic, stats in (meta.get('written_topics') or {}).items():
            assert stats['message_count'] == sum(t == topic for t, _, _ in rows)
            if 'first_receive_steady_ns' in stats:
                assert stats['first_receive_steady_ns'] >= meta['window_start_steady_ns']
                assert stats['last_receive_steady_ns'] <= meta['window_end_steady_ns']
        # Reconstruct monotonic receipt times from the trigger's simultaneous ROS/steady pair.
        tr = meta['triggers'][0]
        offset = tr['receive_ros_ns'] - tr['receive_steady_ns']
        for topic, _, stamp in rows:
            if topic == '/test/sequence' and 'ros_clock_jump' not in meta['errors']:
                assert meta['window_start_steady_ns'] - 2_000_000 <= stamp - offset <= meta['window_end_steady_ns'] + 2_000_000
        info = subprocess.run(['ros2', 'bag', 'info', str(bag)], capture_output=True, text=True, check=True)
        (self.case_dir / (meta['event_id'] + '.info.txt')).write_text(info.stdout)
        return rows, seq

    def record(self, name, details):
        self.results.append({'scenario': name, 'passed': True, **details})
        print('PASS', name, details, flush=True)
        (self.output / 'report.json').write_text(json.dumps(self.results, indent=2))

    def run(self, real_bag):
        self.start('default_5s_10s', 5.0, 10.0)
        # Two independently updated static transforms, both older than the window.
        for child in ['sensor_a', 'sensor_b']:
            tf = TransformStamped(); tf.header.frame_id = 'world'; tf.child_frame_id = child
            tf.transform.rotation.w = 1.0
            self.tf.publish(TFMessage(transforms=[tf]))
            self.wait(0.05)
        self.wait(6.0, True)
        self.fire('single|default window')
        self.wait(10.3, True)
        meta = self.finish()[0]
        rows, seq = self.check(meta)
        frames = {t.child_frame_id for topic, data, _ in rows if topic == '/tf_static'
                  for t in deserialize_message(data, TFMessage).transforms}
        assert frames == {'sensor_a', 'sensor_b'}, frames
        stats = meta['written_topics']['/test/sequence']
        assert stats['first_receive_steady_ns'] - meta['window_start_steady_ns'] < 100_000_000
        assert meta['window_end_steady_ns'] - stats['last_receive_steady_ns'] < 100_000_000
        self.record('default_5s_10s', {'messages': len(seq), 'static_frames': sorted(frames)})
        # Actual ros2 bag play; verify payload sequence and transient-local static context.
        got, tf_got = [], set()
        sub = self.node.create_subscription(String, '/test/replayed', lambda m: got.append(int(m.data)),
            QoSProfile(depth=1000, reliability=ReliabilityPolicy.BEST_EFFORT))
        tf_sub = self.node.create_subscription(TFMessage, '/tf/replayed',
            lambda m: tf_got.update(t.child_frame_id for t in m.transforms),
            QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        playback_log = open(self.case_dir / 'playback.log', 'w')
        player = subprocess.Popen(['ros2', 'bag', 'play', str(self.case_dir / meta['event_id']),
            '--rate', '5', '--delay', '1', '--remap', '/test/sequence:=/test/replayed', '/tf_static:=/tf/replayed'],
            stdout=playback_log, stderr=playback_log)
        deadline = time.monotonic() + 30
        while player.poll() is None:
            assert time.monotonic() < deadline
            rclpy.spin_once(self.node, timeout_sec=0.001)
        self.wait(0.3)
        assert player.returncode == 0
        assert got == seq, (len(got), len(seq))
        assert tf_got == frames, tf_got
        playback_log.close()
        self.node.destroy_subscription(sub); self.node.destroy_subscription(tf_sub)
        self.record('ros2_bag_play_roundtrip', {'messages': len(got), 'static_frames': sorted(tf_got)})

        self.start('merge_dedup')
        self.wait(0.8, True); self.fire('a|first'); self.wait(0.3, True)
        self.fire('a|duplicate'); self.fire('b|extension'); self.wait(0.8, True)
        meta = self.finish()[0]; _, seq = self.check(meta, count_triggers=2)
        assert meta['window_end_steady_ns'] == meta['triggers'][-1]['receive_steady_ns'] + 600_000_000
        self.record('merge_dedup', {'messages': len(seq)})

        self.start('no_followup')
        self.wait(0.8, True); self.fire('silent|no more messages'); self.wait(0.8)
        self.check(self.finish()[0]); self.record('no_followup', {})

        self.start('startup')
        self.fire('early|startup'); self.wait(0.8, True)
        meta = self.finish()[0]; self.check(meta, 'PARTIAL')
        assert 'insufficient_prehistory' in meta['errors']
        self.record('startup', {})

        self.start('separate_events')
        self.wait(0.8, True); self.fire('one|first'); self.wait(0.7, True)
        self.fire('two|second'); self.wait(0.8, True)
        metas = self.finish(2)
        for meta in metas: self.check(meta)
        assert metas[0]['event_id'] != metas[1]['event_id']
        self.record('separate_events', {'events': 2})

        self.start('capacity', extra={'history_bytes': 1024, 'writer_bytes': 1})
        self.wait(0.8, True); self.fire('full|capacity'); self.wait(0.8, True)
        meta = self.finish()[0]; self.check(meta, 'PARTIAL', contiguous=False)
        assert 'insufficient_prehistory' in meta['errors'] and 'writer_queue_overflow' in meta['errors']
        self.record('capacity', {'errors': meta['errors']})

        self.start('disk_failure', extra={'minimum_free_bytes': 9_000_000_000_000_000_000})
        self.wait(0.8, True); self.fire('disk|insufficient disk'); self.wait(0.8)
        meta = self.finish()[0]
        assert meta['status'] == 'FAILED' and not meta['data_complete'], meta
        self.record('disk_failure', {'errors': meta['errors']})

        self.start('shutdown')
        self.wait(0.8, True); self.fire('stop|shutdown'); self.stop()
        meta = yaml.safe_load(next(self.case_dir.glob('*.event.yaml')).read_text())
        self.check(meta, 'PARTIAL'); assert 'shutdown_truncated' in meta['errors']
        self.record('shutdown', {})

        self.start('maximum_duration', extra={'max_event_seconds': 1.2})
        self.wait(0.8, True); self.fire('one|max'); self.wait(0.3, True)
        self.fire('two|max'); self.wait(0.8, True)
        meta = self.finish()[0]; self.check(meta, 'PARTIAL', count_triggers=2)
        assert 'max_event_duration_truncated' in meta['errors']
        self.record('maximum_duration', {})

        self.start('input_overflow', extra={'input_bytes': 2048})
        self.wait(0.8, True); self.fire('input|overload')
        self.pub.publish(String(data='x' * 10000)); self.wait(0.8, True)
        meta = self.finish()[0]; self.check(meta, 'PARTIAL')
        assert 'input_queue_overflow' in meta['errors'] and meta['input_drops_total'] > 0
        self.record('input_overflow', {'input_drops_total': meta['input_drops_total']})

        clock_pub = self.node.create_publisher(Clock, '/clock', 10)
        self.start('clock_jump', extra={'use_sim_time': True})
        clock = Clock(); clock.clock.sec = 100
        clock_pub.publish(clock); self.wait(0.8, True); self.fire('clock|jump')
        clock.clock.sec = 50; clock_pub.publish(clock); self.wait(0.8, True)
        meta = self.finish()[0]; self.check(meta, 'PARTIAL', contiguous=False)
        assert 'ros_clock_jump' in meta['errors']
        assert meta['window_end_steady_ns'] - meta['triggers'][0]['receive_steady_ns'] == 600_000_000
        self.node.destroy_publisher(clock_pub)
        self.record('clock_jump', {'steady_post_window_ns': 600_000_000})

        if real_bag:
            self.start('existing_imu_bag', 5.0, 10.0, topics=['/lidar_imu'], types=['sensor_msgs/msg/Imu'])
            player_log = open(self.case_dir / 'input_playback.log', 'w')
            player = subprocess.Popen(['ros2', 'bag', 'play', str(real_bag), '--topics', '/lidar_imu'],
                                      stdout=player_log, stderr=player_log)
            try:
                self.wait(7.0); self.fire('imu|existing bag'); self.wait(10.3)
                meta = self.finish()[0]; rows, _ = self.check(meta)
                stamps = [deserialize_message(data, Imu).header.stamp for topic, data, _ in rows if topic == '/lidar_imu']
                keys = [s.sec * 1_000_000_000 + s.nanosec for s in stamps]
                assert len(keys) > 2000 and len(set(keys)) == len(keys)
                source = [deserialize_message(data, Imu).header.stamp for topic, data, _ in bag_read(real_bag) if topic == '/lidar_imu']
                original = [s.sec * 1_000_000_000 + s.nanosec for s in source]
                first = original.index(keys[0])
                assert original[first:first + len(keys)] == keys, 'source IMU missing or reordered'
                self.record('existing_imu_bag', {'messages': len(keys), 'source': str(real_bag), 'contiguous_source_slice': True})
            finally:
                if player.poll() is None: player.send_signal(signal.SIGINT)
                player.wait(timeout=10); player_log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'validation' / time.strftime('%Y%m%d_%H%M%S'))
    parser.add_argument('--real-bag', type=Path)
    parser.add_argument('--storage', choices=['sqlite3', 'mcap'], default='sqlite3')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rclpy.init()
    harness = Harness(args.output.resolve(), args.storage)
    try:
        harness.run(args.real_bag)
    finally:
        if hasattr(harness, 'proc') and harness.proc.poll() is None: harness.stop()
        harness.node.destroy_node(); rclpy.shutdown()
    print('All checks passed:', args.output)


if __name__ == '__main__':
    main()

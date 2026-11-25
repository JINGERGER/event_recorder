#!/usr/bin/env python3
"""Measure recorder-only CPU/RSS from /proc, across idle/buffering/writing phases."""
import argparse
from array import array
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import rclpy
from sensor_msgs.msg import Image
from rclpy.qos import QoSProfile, ReliabilityPolicy
import yaml
from validate_rosbag import Harness


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--storage', choices=['sqlite3', 'mcap'], default='sqlite3')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--real-bag', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    rclpy.init()
    harness = Harness(args.output.resolve(), args.storage)
    results = []
    for name in ['imu_200hz', 'image_20mib_s']:
        image_case = name.startswith('image')
        topic = '/benchmark/image' if image_case else '/lidar_imu'
        harness.start(name, 5.0, 10.0,
                      extra={'history_retention_seconds': 8.0, 'reliability': ['reliable']},
                      topics=[topic], types=['sensor_msgs/msg/Image' if image_case else 'sensor_msgs/msg/Imu'])
        samples, phase = [], ['idle']
        done = threading.Event()
        ticks = os.sysconf('SC_CLK_TCK')
        def monitor():
            previous = None
            while not done.wait(0.2):
                try:
                    fields = Path(f'/proc/{harness.proc.pid}/stat').read_text().split()
                    t = time.monotonic()
                    cpu = (int(fields[13]) + int(fields[14])) / ticks
                    rss = int(fields[23]) * os.sysconf('SC_PAGE_SIZE') / 1048576
                    if previous:
                        samples.append(dict(phase=phase[0], cpu_percent_one_core=100*(cpu-previous[1])/(t-previous[0]), rss_mib=rss))
                    previous = (t, cpu)
                except FileNotFoundError:
                    break
        worker = threading.Thread(target=monitor); worker.start()
        player = None
        pub = None
        published = 0
        try:
            harness.wait(3)
            if image_case:
                pub = harness.node.create_publisher(Image, topic,
                    QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE))
                msg = Image(height=1024, width=1024, encoding='mono8', step=1024)
                msg.data = array('B', bytes(1048576))
                harness.wait(0.5)
            else:
                log = open(harness.case_dir / 'player.log', 'w')
                player = subprocess.Popen(['ros2','bag','play',str(args.real_bag),'--topics',topic], stdout=log, stderr=log)
            def feed(duration):
                nonlocal published
                end = time.monotonic() + duration
                next_time = time.monotonic()
                while time.monotonic() < end:
                    if image_case and time.monotonic() >= next_time:
                        msg.header.stamp = harness.node.get_clock().now().to_msg()
                        pub.publish(msg); published += 1
                        next_time += 0.05
                    rclpy.spin_once(harness.node, timeout_sec=0)
                    time.sleep(0.001)
            phase[0] = 'buffering'; feed(9)
            phase[0] = 'recording'; harness.fire('benchmark|load measurement'); feed(10.4)
            phase[0] = 'finalizing'
            meta = harness.finish()[0]
            assert meta['status'] == 'COMPLETED', meta
            summary = {}
            for label in ['idle', 'buffering', 'recording']:
                data = [x for x in samples if x['phase'] == label]
                cpus = sorted(x['cpu_percent_one_core'] for x in data)
                summary[label] = dict(mean_cpu_percent_one_core=sum(cpus)/len(cpus),
                    p95_cpu_percent_one_core=cpus[int((len(cpus)-1)*0.95)],
                    peak_rss_mib=max(x['rss_mib'] for x in data))
            bag = harness.case_dir / meta['event_id']
            count = meta['written_topics'][topic]['message_count']
            if image_case: assert 295 <= count <= 305, f'Image delivery below target: {count}/300'
            row = dict(scenario=name, storage=args.storage, phases=summary,
                       recorded_messages_per_second=count / 15.0,
                       event_status=meta['status'], written_topics=meta['written_topics'],
                       bag_bytes=sum(p.stat().st_size for p in bag.iterdir() if p.is_file()),
                       published_images=published)
            results.append(row)
            (args.output / 'benchmark.json').write_text(json.dumps(results, indent=2))
            (harness.case_dir / 'samples.json').write_text(json.dumps(samples, indent=2))
            print(json.dumps(row), flush=True)
        finally:
            done.set(); worker.join()
            if harness.proc.poll() is None: harness.stop()
            if player:
                player.send_signal(signal.SIGINT); player.wait(timeout=10); log.close()
            if pub: harness.node.destroy_publisher(pub)
    harness.node.destroy_node(); rclpy.shutdown()


if __name__ == '__main__':
    main()

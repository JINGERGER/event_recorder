#!/usr/bin/env python3
"""Replay the real Bunker Mini bag and verify one 20m trigger plus its event bag."""
import argparse
import json
import math
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import time
from urllib.request import urlopen

import rclpy
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from rclpy.serialization import deserialize_message
from nav_msgs.msg import Odometry
from std_msgs.msg import String
from std_srvs.srv import Trigger
import rosbag2_py
import yaml

ROOT = Path(__file__).resolve().parents[1]


def reference(bag):
    info = yaml.safe_load((bag / 'metadata.yaml').read_text())['rosbag2_bagfile_information']
    rows = []
    for file in info['relative_file_paths']:
        with sqlite3.connect((bag / file).as_uri() + '?mode=ro', uri=True) as db:
            topic = db.execute("SELECT id FROM topics WHERE name='/odom'").fetchone()[0]
            rows.extend(db.execute('SELECT timestamp,data FROM messages WHERE topic_id=? ORDER BY timestamp,id', (topic,)))
    rows.sort(key=lambda row: row[0])
    cumulative, maximum = 0.0, 0.0
    previous = origin = crossing = None
    stamps = []; previous_stamp = None
    for i, (stamp, data) in enumerate(rows):
        msg = deserialize_message(data, Odometry)
        point = (msg.pose.pose.position.x, msg.pose.pose.position.y)
        header = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
        assert previous_stamp is None or header > previous_stamp
        assert msg.header.frame_id == 'odom' and msg.child_frame_id == 'base_link'
        if origin is None: origin = point
        if previous is not None: cumulative += math.dist(point, previous)
        maximum = max(maximum, math.dist(point, origin))
        if cumulative > 20 and crossing is None:
            crossing = {'source_index': i, 'distance_m': cumulative, 'source_stamp_ns': header,
                        'offset_from_first_odom_seconds': (stamp-rows[0][0])/1e9}
        stamps.append(header); previous = point; previous_stamp = header
    assert crossing is not None
    return rows, stamps, {'source_messages': len(rows), 'total_path_m': cumulative,
                         'maximum_displacement_m': maximum, 'first_crossing': crossing}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bag', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rate', type=float, default=4.0)
    args = parser.parse_args()
    if not 0 < args.rate <= 4:
        parser.error('rate must be > 0 and <= 4 to retain a full 10s post window in this source bag')
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    source, stamps, expected = reference(args.bag.resolve())
    config = yaml.safe_load((ROOT / 'src/event_recorder/config/bunker_distance_demo.yaml').read_text())
    params = config['event_recorder']['ros__parameters']
    params['output_directory'] = str(output / 'bags'); params['minimum_free_bytes'] = 1
    config_path = output / 'config.yaml'; config_path.write_text(yaml.safe_dump(config))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    rclpy.init(); node = rclpy.create_node('bunker_distance_validation')
    publications, detector = [], {}
    node.create_subscription(String, '/event_recorder/trigger', lambda msg: publications.append(msg.data), 100)
    def on_status(msg):
        detector.clear(); detector.update(json.loads(msg.data))
    node.create_subscription(String, '/odom_distance_trigger/status', on_status, QoSProfile(
        depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    log = open(output / 'launch.log', 'w'); player_log = open(output / 'playback.log', 'w')
    launcher = subprocess.Popen(['ros2','launch','event_recorder','bunker_distance_demo.launch.py',
                                 f'config:={config_path}', f'port:={port}'], stdout=log, stderr=log)
    player = None
    try:
        deadline = time.monotonic()+20
        while True:
            rclpy.spin_once(node, timeout_sec=0.05)
            try:
                with urlopen(f'http://127.0.0.1:{port}/api/state', timeout=1) as response:
                    state = json.load(response)
                if state['online'] and detector.get('state') == 'ARMED': break
            except OSError: pass
            assert launcher.poll() is None and time.monotonic()<deadline
        player = subprocess.Popen(['ros2','bag','play',str(args.bag.resolve()),'--rate',str(args.rate),
            '--delay','1','--disable-keyboard-controls','--topics',
            '/odom','/imu/data_raw','/scan','/tf','/tf_static'], stdout=player_log, stderr=player_log)
        deadline = time.monotonic()+320/args.rate+20
        last_log = time.monotonic()
        while player.poll() is None:
            rclpy.spin_once(node, timeout_sec=0.05)
            assert time.monotonic()<deadline, 'playback timeout'
            if time.monotonic()-last_log>10:
                print(f'Progress: {detector.get("distance_m", 0):.3f} m, {detector.get("state")}', flush=True)
                last_log = time.monotonic()
        assert player.returncode == 0, (output/'playback.log').read_text()
        deadline = time.monotonic()+15
        while True:
            rclpy.spin_once(node, timeout_sec=0.1)
            files = list((output/'bags').glob('*.event.yaml'))
            if len(files)==1:
                meta = yaml.safe_load(files[0].read_text())
                if meta.get('status') in ('COMPLETED','PARTIAL','FAILED') and detector.get('samples')==len(source): break
            assert time.monotonic()<deadline, (detector, files)
        assert len(publications)==1, publications
        rule, request_id, reason = publications[0].split('|', 2)
        detail = json.loads(reason)
        assert rule=='bunker_mini'
        assert detail['source_stamp_ns']==expected['first_crossing']['source_stamp_ns'], detail
        assert abs(detail['distance_m']-expected['first_crossing']['distance_m'])<1e-8
        assert detail['samples']==expected['first_crossing']['source_index']+1
        assert detector['state']=='TRIGGER_PUBLISHED' and detector['error']==''
        assert abs(detector['distance_m']-expected['total_path_m'])<1e-8
        assert meta['status']=='COMPLETED' and meta['data_complete'], meta
        assert meta['trigger_rule']=='bunker_mini' and len(meta['triggers'])==1
        assert meta['triggers'][0]['id']==request_id and meta['triggers'][0]['reason']==reason
        assert meta['window_end_steady_ns']-meta['window_start_steady_ns']==15_000_000_000
        bag = output/'bags'/meta['event_id']
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(bag),storage_id='mcap'),rosbag2_py.ConverterOptions('',''))
        recorded = []; counts = {}
        while reader.has_next():
            topic,data,timestamp = reader.read_next(); counts[topic]=counts.get(topic,0)+1
            if topic=='/odom': recorded.append(data)
        recorded_stamps = []
        for data in recorded:
            msg=deserialize_message(data,Odometry)
            recorded_stamps.append(msg.header.stamp.sec*10**9+msg.header.stamp.nanosec)
        first = stamps.index(recorded_stamps[0])
        assert recorded_stamps==stamps[first:first+len(recorded_stamps)]
        assert recorded==[data for _,data in source[first:first+len(recorded)]]
        assert detail['source_stamp_ns'] in recorded_stamps
        assert set(counts)==set(params['triggers']['bunker_mini']['topics']), counts
        for topic, stats in meta['written_topics'].items():
            assert stats['message_count']==counts[topic]
            if 'first_receive_steady_ns' in stats:
                assert meta['window_start_steady_ns']<=stats['first_receive_steady_ns']
                assert stats['last_receive_steady_ns']<=meta['window_end_steady_ns']
        info=subprocess.run(['ros2','bag','info',str(bag)],capture_output=True,text=True,check=True)
        (output/'bag_info.txt').write_text(info.stdout)
        client=node.create_client(Trigger,'/odom_distance_trigger/reset')
        assert client.wait_for_service(timeout_sec=3)
        result=client.call_async(Trigger.Request())
        rclpy.spin_until_future_complete(node,result,timeout_sec=3)
        assert result.done() and result.result().success
        end=time.monotonic()+2
        while detector.get('state')!='ARMED':
            rclpy.spin_once(node,timeout_sec=0.1); assert time.monotonic()<end
        assert detector['distance_m']==0 and detector['samples']==0
        assert detector['request_id']!=request_id
        report={'passed':True,'source_bag':str(args.bag.resolve()),'playback_rate':args.rate,
                'reference':expected,'trigger_publications':len(publications),'trigger_details':detail,
                'event_id':meta['event_id'],'event_status':meta['status'],'recorded_counts':counts,
                'recorded_odom_is_exact_contiguous_source_slice':True,'window_seconds':15,
                'reset_service_verified':True}
        (output/'report.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2),flush=True)
    finally:
        for process in [player,launcher]:
            if process and process.poll() is None:
                process.send_signal(signal.SIGINT); process.wait(timeout=20)
        log.close();player_log.close();node.destroy_node();rclpy.shutdown()


if __name__=='__main__':
    main()

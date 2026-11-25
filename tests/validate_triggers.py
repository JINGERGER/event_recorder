#!/usr/bin/env python3
"""Named trigger selection, busy rejection, 3s cooldown and scoped loss checks."""
import argparse
import json
from pathlib import Path
import time

import rclpy
from rclpy.serialization import deserialize_message
from std_msgs.msg import String
from tf2_msgs.msg import TFMessage
from geometry_msgs.msg import TransformStamped
import yaml
from validate_rosbag import Harness, bag_read


class TriggerHarness(Harness):
    def __init__(self, output, storage):
        super().__init__(output, storage)
        self.other = self.node.create_publisher(String, '/test/other', 1000)

    def wait(self, seconds, publish=False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if publish:
                msg = String(data=str(self.seq))
                self.pub.publish(msg); self.other.publish(msg); self.seq += 1
            rclpy.spin_once(self.node, timeout_sec=0)
            time.sleep(0.005)

    def named_start(self, case, extra=None):
        config = {
            'trigger_list': ['motion', 'perception'], 'default_trigger': 'motion',
            'triggers.motion.topics': ['/test/sequence', '/tf_static'],
            'triggers.perception.topics': ['/test/other'],
        }
        config.update(extra or {})
        self.start(case, extra=config, topics=['/test/sequence', '/test/other'],
                   types=['std_msgs/msg/String', 'std_msgs/msg/String'])

    def metas(self):
        return [yaml.safe_load(p.read_text()) for p in self.case_dir.glob('*.event.yaml')]

    def run_named(self):
        self.named_start('rule_selection_cooldown')
        transform = TransformStamped(); transform.header.frame_id = 'world'
        transform.child_frame_id = 'test_sensor'; transform.transform.rotation.w = 1.0
        self.tf.publish(TFMessage(transforms=[transform]))
        self.wait(0.8, True)
        self.fire('unknown|bad|reject'); self.fire('motion|malformed')
        assert not self.metas()
        self.fire('motion|shared_id|first')
        self.wait(0.2, True)
        self.fire('perception|shared_id|busy'); self.fire('motion|second_id|busy')
        self.wait(0.6, True)
        metas = self.metas()
        assert len(metas) == 1 and metas[0]['status'] == 'COMPLETED', metas
        first = metas[0]
        assert len(first['triggers']) == 1
        assert first['window_end_steady_ns'] == first['triggers'][0]['receive_steady_ns'] + 600_000_000
        self.fire('perception|shared_id|cooldown')
        # Exercise the actual default 3-second cooldown, including a near-end rejection.
        ready = first['next_trigger_not_before_steady_ns']
        assert ready - first['window_end_steady_ns'] == 3_000_000_000
        self.wait(max(0, (ready - time.monotonic_ns()) / 1e9 - 0.2), True)
        self.fire('perception|shared_id|still_cooling')
        self.wait(max(0, (ready - time.monotonic_ns()) / 1e9 + 0.1), True)
        assert len(self.metas()) == 1, 'rejected trigger was deferred'
        self.fire('motion|shared_id|duplicate_after_cooldown')
        assert len(self.metas()) == 1, 'dedup failed'
        self.fire('perception|shared_id|accepted_after_cooldown')
        self.wait(0.8, True)
        metas = self.finish(2)
        for meta, topic_set in zip(metas, [{'/test/sequence', '/tf_static'}, {'/test/other'}]):
            assert meta['status'] == 'COMPLETED', meta
            assert set(meta['topics']) == topic_set
            assert len(meta['triggers']) == 1
            bag = self.case_dir / meta['event_id']
            bag_meta = yaml.safe_load((bag / 'metadata.yaml').read_text())['rosbag2_bagfile_information']
            assert {t['topic_metadata']['name'] for t in bag_meta['topics_with_message_count']} == topic_set
            rows = bag_read(bag)
            assert {topic for topic, _, _ in rows} == topic_set
            for topic in topic_set - {'/tf_static'}:
                seq = [int(deserialize_message(data, String).data) for name, data, _ in rows if name == topic]
                assert seq and seq == list(range(seq[0], seq[-1] + 1)), 'missing or duplicate sequence'
                stats = meta['written_topics'][topic]
                assert meta['window_start_steady_ns'] <= stats['first_receive_steady_ns']
                assert stats['last_receive_steady_ns'] <= meta['window_end_steady_ns']
        assert [m['trigger_rule'] for m in metas] == ['motion', 'perception']
        log = (self.case_dir / 'recorder.log').read_text()
        for reason in ['unknown_rule', 'invalid_format', 'busy', 'cooldown', 'duplicate_id']:
            assert f'Trigger rejected: {reason}' in log, reason
        self.record('named_selection_and_cooldown', {
            'events': 2, 'cooldown_seconds': 3, 'no_deferred_triggers': True,
            'topic_isolation': True, 'rule_scoped_dedup': True})

        self.named_start('unselected_input_loss', {'input_bytes': 2048})
        self.wait(0.8, True); self.fire('motion|loss|unselected topic overflow')
        self.other.publish(String(data='x' * 10000)); self.wait(0.8, True)
        meta = self.finish()[0]
        self.check(meta)
        assert meta['input_drops_total'] >= 1
        assert 'input_queue_overflow' not in meta['errors']
        self.record('unselected_loss_isolation', {'global_input_drops': meta['input_drops_total']})

        # New rules must resolve exclusively against the configured topic registry.
        for case, override in [
            ('invalid_topic', {'triggers.motion.topics': ['/not_registered']}),
            ('duplicate_rule', {'trigger_list': ['motion', 'motion']}),
        ]:
            try:
                self.named_start(case, override)
            except AssertionError:
                self.proc.wait(timeout=5)
                self.log.close()
                assert self.proc.returncode != 0
                self.record(case, {'startup_rejected': True})
            else:
                self.stop()
                raise AssertionError('invalid configuration accepted')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--storage', choices=['sqlite3', 'mcap'], default='mcap')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rclpy.init()
    harness = TriggerHarness(args.output.resolve(), args.storage)
    try:
        harness.run_named()
    finally:
        if hasattr(harness, 'proc') and harness.proc.poll() is None:
            harness.stop()
        harness.node.destroy_node(); rclpy.shutdown()
    print('Named trigger checks passed:', args.output)


if __name__ == '__main__':
    main()

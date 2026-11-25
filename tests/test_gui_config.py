import copy
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/event_recorder/web'))
from gui_config import ConfigStore, Conflict, validate


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.params = {
            'topics': ['/imu', '/points'], 'types': ['sensor_msgs/msg/Imu', 'sensor_msgs/msg/PointCloud2'],
            'trigger_list': ['motion', 'perception'], 'default_trigger': 'motion',
            'triggers': {'motion': {'topics': ['/imu']}, 'perception': {'topics': ['/points']}},
        }

    def test_nested_rules_and_numeric_normalization(self):
        p = validate(self.params)
        self.assertEqual(p['triggers.motion.topics'], ['/imu'])
        self.assertEqual(p['trigger_cooldown_seconds'], 3.0)
        self.assertEqual(type(p['pre_trigger_seconds']), float)

    def test_invalid_configuration(self):
        cases = [
            {'history_retention_seconds': 2.0}, {'post_trigger_seconds': float('nan')},
            {'history_bytes': -1}, {'history_bytes': True}, {'default_trigger': 'missing'},
            {'types': ['std_msgs/msg/String']}, {'trigger_list': ['motion', 'motion']},
            {'reliability': ['reliable']}, {'storage_id': 'missing'},
            {'triggers': {'motion': {'topics': ['/unknown']}, 'perception': {'topics': ['/points']}}},
            {'topics': ['/imu', '/imu']}, {'unknown_parameter': 4},
        ]
        for patch in cases:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate({**self.params, **patch})

    def test_atomic_save_conflict_backup_and_failed_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'recorder.yaml'
            original = yaml.safe_dump({'event_recorder': {'ros__parameters': self.params}})
            path.write_text(original)
            store = ConfigStore(path); first = store.read()
            changed = copy.deepcopy(first['parameters']); changed['post_trigger_seconds'] = 12.0
            saved = store.save(changed, first['revision'])
            self.assertNotEqual(saved['revision'], first['revision'])
            self.assertNotIn('context_topics: []', path.read_text())
            self.assertEqual(saved['parameters']['context_topics'], [])
            self.assertEqual(path.with_suffix('.yaml.bak').read_text(), original)
            with self.assertRaises(Conflict):
                store.save(first['parameters'], first['revision'])
            before = path.read_text()
            with self.assertRaises(ValueError):
                store.save({**changed, 'history_bytes': -1}, saved['revision'])
            self.assertEqual(before, path.read_text())
            self.assertEqual(store.read()['parameters']['post_trigger_seconds'], 12.0)


if __name__ == '__main__':
    unittest.main()

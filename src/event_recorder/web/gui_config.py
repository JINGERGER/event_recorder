"""Validated, optimistic-concurrency configuration storage for the web UI."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading

import yaml

TIME_DEFAULTS = {
    'pre_trigger_seconds': 5.0, 'post_trigger_seconds': 10.0,
    'history_retention_seconds': 8.0, 'trigger_cooldown_seconds': 3.0,
    'max_event_seconds': 300.0, 'dedup_seconds': 60.0,
}
BUDGET_DEFAULTS = {
    'history_bytes': 268435456, 'input_bytes': 67108864, 'writer_bytes': 536870912,
    'context_bytes': 16777216, 'queue_items': 100000, 'max_pending_events': 8,
    'minimum_free_bytes': 1073741824,
}
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]*\Z')
TOPIC = re.compile(r'(?:/[A-Za-z_][A-Za-z0-9_]*)+\Z')
TYPE = re.compile(r'[A-Za-z][A-Za-z0-9_]*/msg/[A-Za-z][A-Za-z0-9_]*\Z')


class Conflict(ValueError):
    pass


def flatten(values, prefix=''):
    if not isinstance(values, dict):
        raise ValueError('配置必须是参数对象')
    result = {}
    for key, value in values.items():
        if not isinstance(key, str):
            raise ValueError('参数名称必须是字符串')
        name = prefix + key
        children = flatten(value, name + '.') if isinstance(value, dict) else {name: value}
        if result.keys() & children.keys():
            raise ValueError('重复参数名称')
        result.update(children)
    return result


def validate(parameters):
    p = flatten(parameters)
    if len(p) > 1024:
        raise ValueError('参数数量过多')
    for key, default in TIME_DEFAULTS.items():
        value = p.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= 86400:
            raise ValueError(f'{key} 必须是大于 0、不超过 86400 的有限秒数')
        p[key] = float(value)
    if p['history_retention_seconds'] < p['pre_trigger_seconds']:
        raise ValueError('历史保留时间不能小于前置窗口')
    if p['max_event_seconds'] < p['pre_trigger_seconds'] + p['post_trigger_seconds']:
        raise ValueError('最大事件时长不能小于前置与后置窗口之和')
    for key, default in BUDGET_DEFAULTS.items():
        value = p.get(key, default)
        if type(value) is not int or not 0 < value < 2**63:
            raise ValueError(f'{key} 必须是正整数')
        p[key] = value
    p.setdefault('storage_id', 'sqlite3')
    if p['storage_id'] not in ('sqlite3', 'mcap'):
        raise ValueError('存储格式仅支持 sqlite3 / mcap')
    p.setdefault('output_directory', 'event_bags')
    if not isinstance(p['output_directory'], str) or not p['output_directory'].strip() or len(p['output_directory']) > 4096 or '\x00' in p['output_directory']:
        raise ValueError('输出目录无效')
    if 'use_sim_time' in p and type(p['use_sim_time']) is not bool:
        raise ValueError('use_sim_time 必须是布尔值')
    def strings(key, default):
        value = p.get(key, default)
        if not isinstance(value, list) or len(value) > 256 or any(not isinstance(x, str) or len(x) > 512 for x in value):
            raise ValueError(f'{key} 必须是字符串数组，最多 256 项')
        p[key] = value
        return value
    topics = strings('topics', ['/imu/data'])
    types = strings('types', ['sensor_msgs/msg/Imu'])
    contexts = strings('context_topics', [])
    context_types = strings('context_types', [])
    if not topics:
        raise ValueError('至少需要注册一个普通数据 topic')
    if len(topics) != len(types) or len(contexts) != len(context_types):
        raise ValueError('topic 与消息类型数量不一致')
    all_topics = topics + contexts
    if not all_topics or len(set(all_topics)) != len(all_topics) or any(not TOPIC.fullmatch(t) for t in all_topics):
        raise ValueError('topic 必须是唯一的绝对 ROS 名称，例如 /imu/data')
    if any(not TYPE.fullmatch(t) for t in types + context_types):
        raise ValueError('消息类型格式应为 package/msg/Type')
    for key, values in [('reliability', {'reliable', 'best_effort'}), ('durability', {'volatile', 'transient_local'})]:
        qos = strings(key, [])
        if qos and (len(qos) != len(topics) or any(q not in values for q in qos)):
            raise ValueError(f'{key} 应与普通 topic 一一对应')
    names = strings('trigger_list', [])
    if len(set(names)) != len(names) or any(not NAME.fullmatch(n) for n in names):
        raise ValueError('机器人类型名称必须唯一，由字母开头，仅包含字母、数字或下划线')
    for name in names:
        key = f'triggers.{name}.topics'
        selected = strings(key, [])
        if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(all_topics):
            raise ValueError(f'trigger {name} 必须选择已注册且不重复的 topic')
    p.setdefault('default_trigger', names[0] if names else 'default')
    if p['default_trigger'] not in (names or ['default']):
        raise ValueError('默认机器人类型必须存在于 trigger list 中')
    allowed = set(TIME_DEFAULTS) | set(BUDGET_DEFAULTS) | {
        'storage_id', 'output_directory', 'use_sim_time', 'topics', 'types', 'context_topics',
        'context_types', 'reliability', 'durability', 'trigger_list', 'default_trigger',
    } | {f'triggers.{n}.topics' for n in names}
    unknown = set(p) - allowed
    if unknown:
        raise ValueError('不支持的参数: ' + ', '.join(sorted(unknown)))
    return p


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.lock = threading.Lock()
        self.read()

    def _read(self):
        raw = self.path.read_bytes()
        if len(raw) > 1048576:
            raise ValueError('配置文件过大')
        doc = yaml.safe_load(raw)
        if not isinstance(doc, dict) or set(doc) != {'event_recorder'}:
            raise ValueError('配置文件应只包含 event_recorder 节点')
        values = doc['event_recorder']['ros__parameters']
        return {'parameters': validate(values), 'revision': hashlib.sha256(raw).hexdigest(), 'path': str(self.path)}

    def read(self):
        with self.lock:
            return self._read()

    def save(self, parameters, revision):
        params = validate(parameters)
        with self.lock:
            old = self._read()
            if revision != old['revision']:
                raise Conflict('配置已被其他页面或程序修改，请重新加载后再保存')
            # Keep a single previous version and replace the configured file atomically.
            previous = self.path.read_bytes()
            # Humble's parameter YAML parser cannot infer a type for [] overrides.
            # Omit optional empty arrays so the recorder's typed defaults take effect.
            optional_arrays = {'context_topics', 'context_types', 'reliability', 'durability', 'trigger_list'}
            serialized = {k: v for k, v in params.items() if not (k in optional_arrays and v == [])}
            raw = yaml.safe_dump({'event_recorder': {'ros__parameters': serialized}}, allow_unicode=True, sort_keys=False)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, delete=False, encoding='utf-8') as out:
                temp = Path(out.name)
                try:
                    out.write(raw); out.flush(); os.fsync(out.fileno())
                    os.chmod(temp, self.path.stat().st_mode & 0o777)
                    self.path.with_suffix(self.path.suffix + '.bak').write_bytes(previous)
                    os.replace(temp, self.path)
                finally:
                    temp.unlink(missing_ok=True)
            return self._read()

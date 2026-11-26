#!/usr/bin/python3
"""Dependency-light HTTP/ROS bridge; never starts, stops, or owns the recorder."""
import argparse
import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit

import rclpy
from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger
import yaml

from gui_config import ConfigStore, Conflict


class ApiError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class Bridge(Node):
    def __init__(self, recorder):
        super().__init__('event_recorder_web')
        self.recorder = '/' + recorder.strip('/')
        self.lock = threading.Lock()
        self.status = None
        self.received = 0.0
        self.trigger_clients = {}
        self.create_subscription(String, self.recorder + '/status', self.on_status,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))

    def on_status(self, msg):
        try:
            parsed = yaml.safe_load(msg.data)
            if not isinstance(parsed, dict) or 'state' not in parsed:
                return
            with self.lock:
                self.status = parsed
                self.received = time.monotonic()
        except yaml.YAMLError:
            pass

    def snapshot(self):
        with self.lock:
            status = copy.deepcopy(self.status)
            age = time.monotonic() - self.received
        return {'online': bool(status and age < 2.0), 'age_seconds': age if status else None, 'recorder': status}

    def trigger_rule(self, rule):
        snapshot = self.snapshot()
        if not snapshot['online']:
            raise ApiError(503, '录制器离线或状态已超时，未发送触发请求')
        if rule not in snapshot['recorder'].get('rules', {}):
            raise ApiError(400, '运行中的配置没有这种机器人类型')
        with self.lock:
            client = self.trigger_clients.get(rule)
            if client is None:
                client = self.create_client(Trigger, self.recorder + '/triggers/' + rule)
                self.trigger_clients[rule] = client
        if not client.wait_for_service(timeout_sec=0.5):
            raise ApiError(503, '触发服务尚未就绪，未发送请求')
        done = threading.Event()
        future = client.call_async(Trigger.Request())
        future.add_done_callback(lambda _: done.set())
        if not done.wait(5.0):
            client.remove_pending_request(future)
            raise ApiError(504, '触发结果未知：等待回应超时，请检查事件状态；不要自动重复触发')
        try:
            response = future.result()
            detail = yaml.safe_load(response.message)
            if not isinstance(detail, dict):
                raise ValueError('invalid trigger response')
            detail['accepted'] = response.success
            return detail
        except Exception as exc:
            raise ApiError(502, '无法读取录制器回应') from exc


class Application:
    def __init__(self, store, bridge, static, token):
        self.store, self.bridge, self.static, self.token = store, bridge, static, token
        self.event_lock = threading.Lock()
        self.event_cache = (0, '', [])
        self.discovery_lock = threading.Lock()
        self.discovery_cache = (0, [])

    def state(self):
        snapshot = self.bridge.snapshot()
        saved = self.store.read()
        running = (snapshot['recorder'] or {}).get('configuration', {})
        snapshot['needs_restart'] = bool(running) and any(running.get(k) != v for k, v in saved['parameters'].items())
        snapshot['config_path'] = saved['path']
        return snapshot

    def events(self):
        snapshot = self.bridge.snapshot()
        directory = (snapshot['recorder'] or {}).get('output_directory')
        if not directory:
            directory = self.store.read()['parameters']['output_directory']
        directory = Path(directory).expanduser().resolve()
        with self.event_lock:
            when, cached_dir, cached = self.event_cache
            if time.monotonic() - when < 1.0 and cached_dir == str(directory):
                return {'directory': str(directory), 'events': cached}
            events = []
            paths = sorted(directory.glob('*.event.yaml'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
            for path in paths:
                try:
                    if path.stat().st_size > 4 * 1048576:
                        continue
                    meta = yaml.safe_load(path.read_text())
                    if not isinstance(meta, dict):
                        continue
                    event_id = meta.get('event_id', path.stem)
                    events.append({
                        'event_id': str(event_id), 'rule': meta.get('trigger_rule', ''),
                        'status': meta.get('status', 'UNKNOWN'), 'data_complete': meta.get('data_complete'),
                        'errors': meta.get('errors', []), 'modified_at': path.stat().st_mtime,
                        'bag_path': str(directory / str(event_id)),
                        'message_count': sum(int(v.get('message_count', 0)) for v in (meta.get('written_topics') or {}).values()),
                        'topics': list((meta.get('topics') or {}).keys()),
                    })
                except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
                    continue
            self.event_cache = (time.monotonic(), str(directory), events)
            return {'directory': str(directory), 'events': events}

    def topics(self):
        with self.discovery_lock:
            when, cached = self.discovery_cache
            if time.monotonic() - when > 2.0:
                cached = [{'name': name, 'types': types} for name, types in self.bridge.get_topic_names_and_types()]
                self.discovery_cache = (time.monotonic(), cached)
            return {'topics': cached}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'EventRecorderGUI/0.3'

        def log_message(self, fmt, *args):
            if len(args) > 1 and str(args[1]).startswith(('4', '5')):
                super().log_message(fmt, *args)

        def send_data(self, code, data, content_type='application/json; charset=utf-8'):
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if isinstance(data, (dict, list)) else data
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def authorize(self, mutation=False):
            host = self.headers.get('Host', '')
            try:
                hostname = urlsplit('http://' + host).hostname
                if not hostname:
                    raise ValueError()
                if hostname not in {'localhost', socket.gethostname(), socket.getfqdn()}:
                    address = ipaddress.ip_address(hostname)
                    if not app.token and not address.is_loopback:
                        raise ValueError()
            except ValueError:
                raise ApiError(403, '不允许的 Host')
            if app.token and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + app.token):
                raise ApiError(401, '请输入访问密钥')
            if mutation:
                origin = self.headers.get('Origin')
                if origin and origin not in ('http://' + host, 'https://' + host):
                    raise ApiError(403, '不允许跨站请求')
                if self.headers.get_content_type() != 'application/json':
                    raise ApiError(415, '请求必须使用 application/json')

        def do_GET(self):
            try:
                path = urlsplit(self.path).path
                if path.startswith('/api/'):
                    self.authorize()
                    routes = {'/api/state': app.state, '/api/config': app.store.read,
                              '/api/events': app.events, '/api/topics': app.topics}
                    if path not in routes:
                        raise ApiError(404, '接口不存在')
                    return self.send_data(200, routes[path]())
                files = {'/': ('index.html', 'text/html; charset=utf-8'),
                         '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                         '/i18n.js': ('i18n.js', 'text/javascript; charset=utf-8'),
                         '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if path not in files:
                    raise ApiError(404, '页面不存在')
                name, mime = files[path]
                self.send_data(200, (app.static / name).read_bytes(), mime)
            except ApiError as exc:
                self.send_data(exc.code, {'error': exc.message})
            except Exception as exc:
                self.send_data(500, {'error': str(exc)})

        def do_POST(self):
            try:
                self.authorize(mutation=True)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1048576:
                    raise ApiError(413, '请求体为空或超过 1 MiB')
                def invalid_constant(value):
                    raise ValueError('不允许非有限数值: ' + value)
                data = json.loads(self.rfile.read(length), parse_constant=invalid_constant)
                if not isinstance(data, dict):
                    raise ValueError('请求体必须是对象')
                path = urlsplit(self.path).path
                if path == '/api/config':
                    result = app.store.save(data['parameters'], data['revision'])
                    result['restart_required'] = True
                elif path == '/api/trigger':
                    rule = data.get('rule')
                    if not isinstance(rule, str) or len(rule) > 512:
                        raise ValueError('机器人类型名称无效')
                    result = app.bridge.trigger_rule(rule)
                else:
                    raise ApiError(404, '接口不存在')
                self.send_data(200, result)
            except Conflict as exc:
                self.send_data(409, {'error': str(exc)})
            except ApiError as exc:
                self.send_data(exc.code, {'error': exc.message})
            except (ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
                self.send_data(400, {'error': str(exc)})
            except Exception as exc:
                self.send_data(500, {'error': str(exc)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Event recorder web GUI')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--recorder', default='/event_recorder')
    args, ros_args = parser.parse_known_args()
    token = os.environ.get('EVENT_RECORDER_GUI_TOKEN', '')
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not token:
        parser.error('非本机监听需设置 EVENT_RECORDER_GUI_TOKEN；或通过 SSH 端口转发访问默认本机地址')
    store = ConfigStore(args.config)
    static = Path(__file__).resolve().parent / 'static'
    if not static.is_dir():
        from ament_index_python.packages import get_package_share_directory
        static = Path(get_package_share_directory('event_recorder')) / 'web' / 'static'
    rclpy.init(args=ros_args)
    bridge = Bridge(args.recorder)
    executor = rclpy.executors.SingleThreadedExecutor()
    executor.add_node(bridge)
    def spin_ros():
        try:
            executor.spin()
        except ExternalShutdownException:
            pass
    spin = threading.Thread(target=spin_ros, daemon=True); spin.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(Application(store, bridge, static, token)))
    server.daemon_threads = True
    print(f'Event Recorder GUI: http://{args.host}:{args.port} | config: {store.path}', flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        executor.shutdown(timeout_sec=2)
        spin.join(timeout=2)
        bridge.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()

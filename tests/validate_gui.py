#!/usr/bin/env python3
"""Real ROS + HTTP + Chrome UI verification. Playwright is a test-only dependency."""
import argparse
import copy
import json
import os
from pathlib import Path
import signal
import re
import socket
import subprocess
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import rclpy
from std_msgs.msg import String
import yaml
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chrome', default='/usr/bin/google-chrome')
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    config = output / 'recorder.yaml'
    config.write_text(yaml.safe_dump({'event_recorder': {'ros__parameters': {
        'pre_trigger_seconds': 0.5, 'post_trigger_seconds': 1.2, 'history_retention_seconds': 2.0,
        'trigger_cooldown_seconds': 3.0, 'minimum_free_bytes': 1,
        'output_directory': str(output / 'bags'), 'storage_id': 'mcap',
        'topics': ['/test/sequence', '/test/other'], 'types': ['std_msgs/msg/String', 'std_msgs/msg/String'],
        'trigger_list': ['motion', 'perception', 'on'], 'default_trigger': 'motion',
        'triggers.motion.topics': ['/test/sequence'], 'triggers.perception.topics': ['/test/other'],
        'triggers.on.topics': ['/test/sequence'],
    }}}))
    report = []
    def passed(name, **details):
        report.append({'scenario': name, 'passed': True, **details})
        (output / 'report.json').write_text(json.dumps(report, indent=2))
        print('PASS', name, details, flush=True)
    common_headers = {}
    def request(path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        req = Request(base + path, body, headers={'Content-Type': 'application/json', **common_headers, **(headers or {})})
        try:
            with urlopen(req, timeout=8) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)
    rclpy.init(); node = rclpy.create_node('web_gui_validation_source')
    publishers = [node.create_publisher(String, topic, 100) for topic in ['/test/sequence', '/test/other']]
    count = [0]
    def publish():
        for pub in publishers:
            pub.publish(String(data=str(count[0])))
        count[0] += 1
    node.create_timer(0.01, publish)
    spin = threading.Thread(target=rclpy.spin, args=(node,), daemon=True); spin.start()
    logfile = open(output / 'launch.log', 'w')
    process = subprocess.Popen(['ros2', 'launch', 'event_recorder', 'gui.launch.py',
        f'config:={config}', f'port:={port}'], stdout=logfile, stderr=logfile, start_new_session=True)
    def stop():
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=20)
    try:
        end = time.monotonic() + 20
        while True:
            assert process.poll() is None, 'launch exited; inspect launch.log'
            try:
                code, snapshot = request('/api/state')
                if code == 200 and snapshot['online']:
                    break
            except OSError:
                pass
            assert time.monotonic() < end, 'GUI / ROS connection timeout'
            time.sleep(0.1)
        assert not snapshot['needs_restart'], snapshot
        passed('launch_and_live_status', state=snapshot['recorder']['state'])
        original = request('/api/config')[1]
        invalid = copy.deepcopy(original['parameters']); invalid['history_bytes'] = -1
        assert request('/api/config', {'parameters': invalid, 'revision': original['revision']})[0] == 400
        assert request('/api/config')[1]['revision'] == original['revision']
        assert request('/api/trigger', {'rule': 'motion'}, {'Origin': 'https://untrusted.invalid'})[0] == 403
        assert request('/api/trigger', {'rule': 'missing'})[0] == 400
        assert request('/api/state', headers={'Host': 'untrusted.invalid'})[0] == 403
        passed('invalid_config_and_request_rejection')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chrome, headless=True,
                ignore_default_args=['--headless=old'],
                args=['--headless=new', '--no-sandbox', '--disable-dev-shm-usage'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1080}, device_scale_factor=1, locale='zh-CN')
            errors = []
            page.on('pageerror', lambda error: (errors.append(str(error)), print('BROWSER ERROR:', error, flush=True)))
            page.goto(base)
            expect(page.locator('#connection')).to_have_text('录制器已连接', timeout=10000)
            (output / 'state-debug.json').write_text(json.dumps(request('/api/state')[1], indent=2))
            page.screenshot(path=str(output / 'initial.png'), full_page=True)
            expect(page.locator('.trigger-button')).to_have_count(3)
            expect(page.locator('[data-rule="motion"]')).to_be_enabled()
            page.screenshot(path=str(output / 'overview.png'), full_page=True)
            # Language switching must not mutate ROS/config values or require a reload.
            expect(page.locator('html')).to_have_attribute('lang', 'zh-CN')
            page.locator('#language-select').select_option('en')
            expect(page.locator('#connection')).to_have_text('Recorder connected')
            expect(page.locator('#state-title')).to_have_text('Ready')
            expect(page.locator('[data-rule="motion"]')).to_have_text('Trigger recording →')
            page.screenshot(path=str(output / 'overview-en.png'), full_page=True)
            page.locator('[data-tab="events"]').click()
            expect(page.locator('#event-list')).to_contain_text('No events yet.')
            page.locator('[data-tab="configuration"]').click()
            expect(page.locator('#save-config')).to_have_text('Save configuration')
            # All marked static labels and generated accessibility labels have
            # translations; diagnostics retain exact parameter/user values.
            untranslated = page.evaluate("""() => [...document.querySelectorAll('[data-i18n], [data-i18n-aria-label]')]
                .map(n => n.dataset.i18n || n.dataset.i18nAriaLabel)
                .filter(key => /[\u4e00-\u9fff]/.test(t(key)))""")
            assert not untranslated, untranslated
            assert page.evaluate("t('history_bytes 必须是正整数')") == 'history_bytes must be a positive integer'
            assert page.evaluate("t('trigger go2 必须选择已注册且不重复的 topic')") == 'Trigger go2 must select registered topics without duplicates'
            assert page.evaluate("t('constructor')") == 'constructor'
            assert page.evaluate("t('已接受 {rule}，事件 {id}', {rule:'<b>{id}</b>',id:'original'})") == 'Accepted <b>{id}</b>, event original'

            expect(page.locator('.topic-name').first).to_have_attribute('aria-label', 'Topic name')
            page.screenshot(path=str(output / 'configuration-en.png'), full_page=True)
            assert request('/api/config')[1]['revision'] == original['revision']
            page.reload()
            expect(page.locator('html')).to_have_attribute('lang', 'en')
            expect(page.locator('#connection')).to_have_text('Recorder connected')
            assert page.evaluate('navigator.language') == 'zh-CN'
            page.locator('#language-select').select_option('zh-CN')
            expect(page.locator('#state-title')).to_have_text('准备就绪')
            passed('language_browser_default_switch_and_persistence')

            page.locator('[data-rule="motion"]').click()
            expect(page.locator('#notice')).to_contain_text('已接受 motion')
            expect(page.locator('#state-title')).to_have_text('录制中')
            expect(page.locator('[data-rule="perception"]')).to_be_disabled()
            busy = request('/api/trigger', {'rule': 'perception'})[1]
            assert busy['accepted'] is False and busy['reason'] == 'busy', busy
            expect(page.locator('#state-title')).to_have_text('冷却中', timeout=5000)
            cooldown = request('/api/trigger', {'rule': 'perception'})[1]
            assert cooldown['accepted'] is False and cooldown['reason'] == 'cooldown', cooldown
            expect(page.locator('#state-title')).to_have_text('准备就绪', timeout=5000)
            page.locator('[data-rule="perception"]').click()
            expect(page.locator('#notice')).to_contain_text('已接受 perception')
            page.locator('#language-select').select_option('en')
            expect(page.locator('#notice')).to_contain_text('Accepted perception, event ')
            expect(page.locator('[data-rule="motion"]')).to_be_disabled()

            page.locator('[data-tab="events"]').click()
            expect(page.locator('#event-list')).to_contain_text('motion')
            expect(page.locator('#event-list')).to_contain_text('COMPLETED', timeout=5000)
            expect(page.locator('#event-list')).to_contain_text('Messages:')
            page.locator('#language-select').select_option('zh-CN')
            passed('browser_trigger_actual_feedback_and_cooldown')
            page.locator('[data-tab="configuration"]').click()
            page.locator('#discover').click()
            expect(page.locator('#discovered')).to_contain_text('/test/other')
            page.locator('#post_trigger_seconds').fill('1.8')
            # Preserve unsaved values, added rows, selections and current tab.
            page.locator('#add-rule').click()
            page.locator('.rule-name').last.fill('unsaved_robot')
            draft = page.evaluate('collectConfig()')
            page.locator('#language-select').select_option('en')
            expect(page.locator('#configuration')).to_be_visible()
            expect(page.locator('#config-dirty')).to_have_text('Unsaved changes')
            assert page.evaluate('collectConfig()') == draft
            page.locator('#language-select').select_option('zh-CN')
            assert page.evaluate('collectConfig()') == draft
            page.locator('#rule-rows .remove-button').last.click()
            # Exercise a real server validation error in English, then translate
            # the same notice back without another save request.
            page.locator('#language-select').select_option('en')
            page.locator('#history_retention_seconds').fill('0.1')
            page.locator('#save-config').click()
            expect(page.locator('#notice')).to_have_text('History retention must be at least the pre-trigger window')
            assert request('/api/config')[1]['revision'] == original['revision']
            page.locator('#language-select').select_option('zh-CN')
            expect(page.locator('#notice')).to_have_text('历史保留时间不能小于前置窗口')
            page.locator('#history_retention_seconds').fill('2.0')
            passed('language_preserves_unsaved_form_and_translates_api_errors')

            page.locator('#save-config').click()
            expect(page.locator('#save-state')).to_have_text('保存成功 · 重启 recorder 后生效')
            snapshot = request('/api/state')[1]
            assert snapshot['needs_restart']
            assert snapshot['recorder']['configuration']['post_trigger_seconds'] == 1.2
            assert request('/api/config')[1]['parameters']['post_trigger_seconds'] == 1.8
            assert request('/api/config', {'parameters': original['parameters'], 'revision': original['revision']})[0] == 409
            page.screenshot(path=str(output / 'configuration.png'), full_page=True)
            passed('browser_config_save_pending_restart_and_conflict')
            page.locator('#language-select').select_option('en')
            expect(page.locator('#save-state')).to_have_text('Saved · restart the recorder to apply')
            expect(page.locator('#notice')).to_contain_text('Configuration saved;')
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'English configuration overflow'
            page.screenshot(path=str(output / 'configuration-mobile-en.png'), full_page=True)
            page.locator('[data-tab="overview"]').click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'English overview overflow'
            page.screenshot(path=str(output / 'mobile-en.png'), full_page=True)
            page.locator('#language-select').select_option('zh-CN')

            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('[data-tab="overview"]').click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'mobile horizontal overflow'
            page.screenshot(path=str(output / 'mobile.png'), full_page=True)
            assert not errors, errors
            passed('desktop_mobile_rendering', javascript_errors=errors)
            expect(page.locator('#state-title')).to_have_text('准备就绪', timeout=7000)
            accepted = request('/api/trigger', {'rule': 'motion'})[1]
            assert accepted['accepted']
            browser.close()
            time.sleep(1.6)
            snapshot = request('/api/state')[1]
            assert snapshot['online'] and snapshot['recorder']['last_event']['event_id'] == accepted['event_id']
            assert snapshot['recorder']['last_event']['status'] == 'COMPLETED'
            passed('browser_close_does_not_stop_recording')
        stop()
        logfile.close()
        logfile = open(output / 'restart.log', 'w')
        process = subprocess.Popen(['ros2', 'launch', 'event_recorder', 'gui.launch.py',
            f'config:={config}', f'port:={port}'], stdout=logfile, stderr=logfile, start_new_session=True,
            env={**os.environ, 'EVENT_RECORDER_GUI_TOKEN': 'gui-test-only-token'})
        common_headers['Authorization'] = 'Bearer gui-test-only-token'
        end = time.monotonic() + 20
        while True:
            try:
                state = request('/api/state')[1]
                if state.get('online') and state['recorder']['configuration']['post_trigger_seconds'] == 1.8:
                    break
            except OSError:
                pass
            assert time.monotonic() < end
            time.sleep(0.1)
        assert not state['needs_restart']
        passed('saved_configuration_applies_after_restart')
        assert request('/api/state', headers={'Authorization': ''})[0] == 401
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chrome, headless=True,
                ignore_default_args=['--headless=old'],
                args=['--headless=new', '--no-sandbox', '--disable-dev-shm-usage'])
            page = browser.new_page(viewport={'width': 1280, 'height': 900}, locale='en-US')
            page.goto(base)
            expect(page.locator('#auth-dialog')).to_be_visible()
            expect(page.locator('html')).to_have_attribute('lang', 'en')
            expect(page.locator('#auth-dialog h2')).to_have_text('Connect to console')
            page.locator('#access-token').fill('gui-test-only-token')
            page.locator('#auth-form button').click()
            expect(page.locator('#connection')).to_have_text('Recorder connected')
            assert 'on' in request('/api/state')[1]['recorder']['rules']
            passed('authenticated_browser_access')
            pid = int(re.search(r'\[recorder-1\]: process started with pid \[(\d+)\]', (output / 'restart.log').read_text()).group(1))
            os.kill(pid, signal.SIGINT)
            expect(page.locator('#connection')).to_have_text('Recorder offline', timeout=7000)
            for button in page.locator('.trigger-button').all():
                expect(button).to_be_disabled()
            assert request('/api/trigger', {'rule': 'motion'})[0] == 503
            browser.close()
            passed('offline_detection_and_trigger_blocking')
    finally:
        stop(); logfile.close()
        rclpy.shutdown(); spin.join(timeout=3); node.destroy_node()
    print('GUI validation passed:', output)


if __name__ == '__main__':
    main()

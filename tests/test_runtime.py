import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import runtime
from tray import Tray, ITEM_XML, MENU_XML
from gi.repository import GLib, Gio


class RuntimeTests(unittest.TestCase):
    def test_switch_failure_is_returned(self):
        result = subprocess.CompletedProcess([], 7, 'detail', 'failed')
        with patch.object(runtime, 'command', return_value=result):
            self.assertIs(runtime.run_switch(True), result)

    def test_timeout_is_actionable(self):
        result = runtime.command(['/bin/sh', '-c', 'sleep 5 & wait'], timeout=.05)
        self.assertEqual(result.returncode, 124)

    def test_missing_command_is_actionable(self):
        self.assertEqual(runtime.command(['/does-not-exist/opk']).returncode, 127)

    def status(self, core, mode='none', host='127.0.0.1', port=None, group=True):
        def setting(schema, key):
            return host if key == 'host' else port or runtime.MIXED_PORT
        with patch.dict(runtime.ENV, {'STATE_VERSION': '1'}, clear=True), \
             patch.object(runtime, 'unit_active', side_effect=lambda u: core and u == runtime.UNITS['mihomo']), \
             patch.object(runtime, 'sys_proxy_mode', return_value=mode), \
             patch.object(runtime, 'proxy_setting', side_effect=setting), \
             patch.object(runtime, 'count_injected', return_value=0), \
             patch.object(runtime, 'mihomo_get', return_value={'all': ['A'], 'now': 'A'} if group else None):
            return runtime.query_status()

    def test_stopped_state(self):
        self.assertEqual(self.status(False)['phase'], 'off')

    def test_running_requires_correct_proxy_and_api(self):
        self.assertTrue(self.status(True, 'manual')['running'])
        self.assertFalse(self.status(True, 'manual', port='1')['running'])
        self.assertFalse(self.status(True, 'manual', host='other')['running'])
        self.assertFalse(self.status(True, 'manual', group=False)['running'])

    def test_external_proxy_is_not_owned(self):
        status = self.status(False, 'manual', port='1')
        self.assertFalse(status['proxy_owned'])
        self.assertEqual(status['phase'], 'off')

    def test_dead_owned_proxy_is_partial(self):
        self.assertEqual(self.status(False, 'manual')['phase'], 'partial')

    def test_auxiliary_process_is_not_reported_as_stopped(self):
        with patch.object(runtime, 'unit_active', side_effect=lambda unit: unit == runtime.UNITS['controller']), \
             patch.object(runtime, 'sys_proxy_mode', return_value='none'), \
             patch.object(runtime, 'mihomo_get', return_value=None), \
             patch.object(runtime, 'count_injected', return_value=0):
            self.assertEqual(runtime.query_status()['phase'], 'partial')

    def test_redacts_credentials(self):
        for value in ['Authorization: Bearer abc123', 'password="a b c"',
                      '{"token":"abc123"}', 'secret=abc123', 'https://example.org/sub?secret=abc123']:
            redacted = runtime.redact(value)
            self.assertNotIn('abc123', redacted)
            self.assertNotIn('a b c', redacted)

    def test_logs_bounded_filter_and_clear(self):
        store = runtime.LogStore(limit=3)
        store.add('操作', 'old')
        store.add('操作', 'first', 'ERROR')
        store.add('mihomo', 'SECOND')
        store.add('操作', 'third')
        self.assertEqual(len(store.snapshot()), 3)
        self.assertEqual(len(store.snapshot(level='ERROR')), 1)
        self.assertEqual(len(store.snapshot(source='操作')), 2)
        self.assertEqual(len(store.snapshot(query='second')), 1)
        store.clear()
        self.assertEqual(store.snapshot(), [])

    def test_journal_source_priority_and_cursor(self):
        store = runtime.LogStore()
        reader = runtime.LogFollower(store)
        reader._record(json.dumps({'__CURSOR': 'cursor', '_SYSTEMD_USER_UNIT': runtime.UNITS['controller'],
            'PRIORITY': '3', 'MESSAGE': 'token=abc123', '__REALTIME_TIMESTAMP': '1000000'}))
        entry = store.snapshot()[0]
        self.assertEqual((entry.source, entry.level), ('controller', 'ERROR'))
        self.assertNotIn('abc123', entry.message)
        self.assertEqual(reader.cursor, 'cursor')

    def test_guard_rotation_and_partial_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'guard.log'
            store = runtime.LogStore()
            reader = runtime.LogFollower(store)
            with patch.dict(runtime.ENV, {'GUARD_LOG': str(path)}):
                path.write_text('one\npartial')
                reader._guard_file()
                self.assertEqual([e.message for e in store.snapshot()], ['one'])
                with path.open('a') as stream:
                    stream.write(' end\n')
                reader._guard_file()
                self.assertEqual(store.snapshot()[-1].message, 'partial end')
                path.rename(Path(tmp) / 'old')
                path.write_text('rotated\n')
                reader._guard_file()
                self.assertEqual(store.snapshot()[-1].message, 'rotated')

    def test_component_level_overrides_stdout_priority(self):
        store = runtime.LogStore()
        reader = runtime.LogFollower(store)
        for message in ['{"level":"error","event":"failed"}', 'level=warning msg=failed']:
            reader._record(json.dumps({'PRIORITY': '6', 'MESSAGE': message}))
        self.assertEqual([entry.level for entry in store.snapshot()], ['ERROR', 'WARN'])

    def test_log_reader_stops_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'journalctl'
            script.write_text('#!/usr/bin/python3\nimport time\nprint(\'{"MESSAGE":"ready"}\', flush=True)\ntime.sleep(60)\n')
            script.chmod(0o755)
            reader = runtime.LogFollower(runtime.LogStore())
            with patch.dict(os.environ, {'PATH': tmp}):
                reader.start()
                deadline = time.monotonic() + 3
                while not reader.store.snapshot() and time.monotonic() < deadline:
                    time.sleep(.02)
                process = reader.process
                self.assertIsNotNone(process)
                reader.stop()
                self.assertFalse(reader.thread.is_alive())
                self.assertIsNotNone(process.poll())
                self.assertIsNone(reader.process)

    def test_tray_menu_shapes_and_disabled_actions(self):
        actions = []
        tray = Tray(actions.append, lambda _: None)
        GLib.Variant('(u(ia{sv}av))', (0, tray._layout(0, -1, [])))
        self.assertTrue(Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces)
        self.assertTrue(Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces)
        tray._event(3, 'clicked')
        self.assertEqual(actions, ['toggle'])
        tray.update('busy', '正在停止')
        tray._event(3, 'clicked')
        tray._event(5, 'clicked')
        self.assertEqual(actions, ['toggle'])

    def test_tray_icons_are_argb_pixmaps(self):
        tray = Tray(lambda _: None, lambda _: None)
        for phase in ('on', 'off', 'partial', 'busy'):
            tray.phase = phase
            width, height, pixels = tray._pixmap()[0]
            self.assertEqual(len(pixels), width * height * 4)


if __name__ == '__main__':
    unittest.main()

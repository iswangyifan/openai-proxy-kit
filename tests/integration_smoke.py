"""独立 session bus 托盘往返，以及本项目路由引擎的只读影子探测。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from gi.repository import Gio, GLib
from tray import Tray, WATCHER, ITEM_PATH, ITEM_IFACE, MENU_PATH, MENU_IFACE


class IntegrationTests(unittest.TestCase):
    def test_real_bus_menu_registration_and_events(self):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        xml = '''<node><interface name="org.kde.StatusNotifierWatcher">
          <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
          <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
          <signal name="StatusNotifierItemRegistered"><arg type="s"/></signal>
        </interface></node>'''
        registrations, actions = [], []

        def register(_b, _s, _p, _i, _m, params, invocation):
            registrations.append(params.unpack()[0])
            invocation.return_value(None)
            bus.emit_signal(None, '/StatusNotifierWatcher', WATCHER, 'StatusNotifierItemRegistered',
                            GLib.Variant('(s)', (registrations[-1],)))

        obj = bus.register_object('/StatusNotifierWatcher', Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0],
                                  register, lambda *_: GLib.Variant('b', True), None)
        owner = Gio.bus_own_name_on_connection(bus, WATCHER, Gio.BusNameOwnerFlags.NONE, None, None)
        loop = GLib.MainLoop()
        thread = threading.Thread(target=loop.run, daemon=True)
        thread.start()
        tray = Tray(actions.append, lambda _: None)
        tray.start()
        client = Gio.DBusConnection.new_for_address_sync(os.environ['DBUS_SESSION_BUS_ADDRESS'],
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        try:
            deadline = time.monotonic() + 3
            while not tray.available and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(tray.available)
            self.assertEqual(len(registrations), 1, '注册信号不能触发递归注册')

            def call(path, interface, method, params=None):
                return client.call_sync(bus.get_unique_name(), path, interface, method, params,
                                        None, Gio.DBusCallFlags.NONE, 2000, None)
            layout = call(MENU_PATH, MENU_IFACE, 'GetLayout', GLib.Variant('(iias)', (0, -1, []))).unpack()
            self.assertEqual(len(layout[1][2]), 5)
            call(MENU_PATH, MENU_IFACE, 'Event', GLib.Variant('(isvu)', (3, 'clicked', GLib.Variant('s', ''), 0)))
            self.assertEqual(actions, ['toggle'])
            icon = call(ITEM_PATH, 'org.freedesktop.DBus.Properties', 'Get',
                        GLib.Variant('(ss)', (ITEM_IFACE, 'IconPixmap'))).unpack()[0]
            self.assertEqual(icon[0][0:2], (48, 48))
            tray.update('busy', '测试处理中')
            call(MENU_PATH, MENU_IFACE, 'Event', GLib.Variant('(isvu)', (5, 'clicked', GLib.Variant('s', ''), 0)))
            self.assertEqual(actions, ['toggle'])
            Gio.bus_unown_name(owner)
            owner = 0
            deadline = time.monotonic() + 2
            while tray.available and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(tray.available)
        finally:
            tray.close()
            client.close_sync(None)
            if owner:
                Gio.bus_unown_name(owner)
            bus.unregister_object(obj)
            loop.quit()
            thread.join(2)

    def test_project_engine_shadow_without_extra_runtime(self):
        binary = Path(os.environ.get('OPK_TEST_CONTROLLER', ROOT / 'scripts/route_engine.py'))
        self.assertTrue(binary.is_file())
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = unquote(urlparse(self.path).path)
                requests.append(path)
                if path == '/proxies/Test':
                    value = {'type': 'Selector', 'now': 'JP-Test', 'all': ['JP-Test']}
                elif path == '/connections':
                    value = {'connections': []}
                elif path == '/proxies':
                    value = {'proxies': {'JP-Test': {'alive': True}}}
                elif path == '/proxies/JP-Test':
                    value = {'alive': True}
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                curl = Path(tmp) / 'curl'
                curl.write_text("#!/usr/bin/python3\nimport pathlib,sys\na=sys.argv[1:]\np=pathlib.Path(a[a.index('--output')+1])\np.write_text('ip=127.0.0.1\\nloc=ZZ\\n' if a[-1].endswith('/trace') else 'User-agent: *\\n')\nprint('200',end='')\n")
                curl.chmod(0o755)
                env = dict(os.environ, PATH='/nonexistent', CURL_PATH=str(curl), OPENAI_GROUP='Test',
                           MIHOMO_API=f'http://127.0.0.1:{server.server_port}', MIHOMO_PROXY='http://127.0.0.1:1',
                           MIHOMO_SOCKET='', MIHOMO_SECRET='', STATE_PATH=str(Path(tmp) / 'state.json'), LOG_PATH='')
                mapping = Path(tmp) / 'map.json'
                mapping.write_text(json.dumps({'generation': 'a' * 64, 'bindings': {'JP-Test': 17900}}))
                env['PROBE_MAP'] = str(mapping)
                version = subprocess.run(['/usr/bin/python3', str(binary), '--version'], env=env, capture_output=True, text=True, timeout=5)
                self.assertIn('route engine', version.stdout)
                result = subprocess.run(['/usr/bin/python3', str(binary), '--once', '--shadow'], env=env,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertIn('route_engine_started', result.stdout)
                self.assertFalse((Path(tmp) / 'state.json').exists())
                events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
                self.assertTrue(any(e.get('event') == 'route_current_probe' and e.get('ok') for e in events))
                self.assertIn('/proxies/Test', requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()

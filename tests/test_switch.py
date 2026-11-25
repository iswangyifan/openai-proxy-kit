"""使用隔离 HOME、API 与系统命令替身验证启停，不触碰真实系统服务。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
STUB = '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = Path(os.environ['OPK_STUB_STATE'])
state = json.loads(state_path.read_text())
with open(os.environ['OPK_STUB_CALLS'], 'a') as stream:
    stream.write(name + ' ' + ' '.join(args) + '\\n')
def save():
    state_path.write_text(json.dumps(state))
if name == 'systemctl':
    unit = args[-1] if args else ''
    if 'is-active' in args:
        sys.exit(0 if state.get('active') else 3)
    if 'show-environment' in args:
        sys.exit(0)
    if 'show' in args:
        print('42')
    if 'is-enabled' in args:
        print('enabled')
    if 'enable' in args:
        if 'core.service' in args and state.get('start_fail'):
            sys.exit(1)
        if 'mihomo-kit.service' in args and state.get('start_fail'):
            sys.exit(1)
        if 'controller.service' in args and state.get('controller_start_fail'):
            sys.exit(1)
        if 'opk-route.service' in args and state.get('controller_start_fail'):
            sys.exit(1)
        if unit in ('core.service', 'mihomo-kit.service'):
            was_active = state.get('active', False)
            state['active'] = True
            state['listening'] = True
            if not was_active:
                state['runtime_mode'] = 'direct'
            save()
    if 'restart' in args and unit in ('core.service', 'mihomo-kit.service'):
        state['active'] = True
        state['listening'] = True
        save()
    if 'disable' in args and state.get('stop_fail'):
        sys.exit(1)
    if ('disable' in args or 'stop' in args) and unit in ('core.service', 'mihomo-kit.service'):
        state['active'] = False
        state['listening'] = False
        save()
elif name == 'ss' and state.get('listening'):
    print('LISTEN 0 128 127.0.0.1:17890 *:* users:(("mihomo",pid=%s,fd=7))' % state.get('pid', 42))
elif name == 'gsettings':
    protocol = args[1].rsplit('.', 1)[-1]
    key = args[2]
    statekey = protocol + '_' + key if protocol in ('http', 'https', 'ftp', 'socks') else key
    if args[0] == 'get':
        default = state.get('host', '127.0.0.1') if key == 'host' else state.get('port', 17890) if key == 'port' else 'manual'
        value = state.get(statekey, default)
        print(value if key == 'port' else "'%s'" % value)
    else:
        state[statekey] = args[3].strip("'")
        save()
elif name == 'loginctl' and 'show-user' in args:
    print('Linger=yes')

'''


class ApiHandler(BaseHTTPRequestHandler):
    def _load(self):
        return json.loads(self.server.state_path.read_text())

    def _save(self, state):
        self.server.state_path.write_text(json.dumps(state))

    def _json(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/configs':
            state = self._load()
            if state.get('api_fail'):
                self._json({}, 500)
            else:
                self._json({'mode': state.get('runtime_mode', 'rule')})
        else:
            self._json({})

    def do_PATCH(self):
        length = int(self.headers.get('Content-Length', '0'))
        value = json.loads(self.rfile.read(length))
        state = self._load()
        if state.get('api_fail'):
            self._json({}, 500)
            return
        if self.path == '/configs':
            state['runtime_mode'] = value['mode']
            self._save(state)
        self._json({})

    def do_PUT(self):
        length = int(self.headers.get('Content-Length', '0'))
        self.rfile.read(length)
        if self._load().get('api_fail'):
            self._json({}, 500)
        else:
            self._json({})

    def log_message(self, *args):
        pass


class SwitchTests(unittest.TestCase):
    def run_switch(self, action, **state):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'bin'
            binary.mkdir()
            for name in ['systemctl', 'ss', 'gsettings', 'sleep']:
                (binary / name).write_text(STUB)
                (binary / name).chmod(0o755)
            units = root / '.config/systemd/user'
            units.mkdir(parents=True)
            for unit in ['core.service', 'controller.service', 'guard.timer', 'guard.service']:
                if unit == 'core.service' and state.get('missing_unit_file'):
                    continue
                (units / unit).touch()
            saved_mode = state.get('saved_mode', 'rule')
            state.setdefault('runtime_mode', saved_mode)
            (root / 'state').write_text(json.dumps(state))
            conf = root / 'conf'
            conf.mkdir()
            config = conf / 'config.yaml'
            config.write_text(yaml.safe_dump({'mode': saved_mode, 'dns': {'enable': saved_mode == 'rule'}, 'rules': ['MATCH,DIRECT']}))
            server = ThreadingHTTPServer(('127.0.0.1', 0), ApiHandler)
            server.state_path = root / 'state'
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            envfile = root / 'kit.env'
            envfile.write_text(
                f"INSTALL_HOME='{root}'\nSTATE_VERSION='2'\nMIXED_PORT='17890'\nAPI_PORT='{server.server_port}'\n"
                f"MIHOMO_CONF_DIR='{conf}'\nMIHOMO_BIN='/usr/bin/true'\nMIHOMO_SECRET='synthetic'\n"
                "UNIT_MIHOMO='core.service'\nUNIT_CONTROLLER='controller.service'\n"
                "UNIT_GUARD_TIMER='guard.timer'\nUNIT_GUARD_SERVICE='guard.service'\n")
            env = dict(os.environ, HOME=tmp, KIT_ENV=str(envfile), PATH=str(binary) + ':' + os.environ['PATH'],
                       OPK_STUB_STATE=str(root / 'state'), OPK_STUB_CALLS=str(root / 'calls'))
            try:
                for current in (action if isinstance(action, (list, tuple)) else [action]):
                    result = subprocess.run(['bash', str(ROOT / 'scripts/switch.sh'), current], env=env,
                                            capture_output=True, text=True, timeout=15)
                calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
                final_state = json.loads((root / 'state').read_text())
                final_config = yaml.safe_load(config.read_text())
                return result, calls, final_state, final_config
            finally:
                server.shutdown()
                worker.join(timeout=5)
                server.server_close()

    def test_foreign_listener_blocks_before_mutation(self):
        result, calls, _, config = self.run_switch('start', listening=True, active=False, pid=99)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(' enable ', calls)
        self.assertNotIn('gsettings set', calls)
        self.assertEqual(config['mode'], 'rule')

    def test_core_start_failure_never_sets_proxy(self):
        result, calls, state, _ = self.run_switch('start', listening=False, start_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('gsettings set', calls)
        self.assertFalse(state['active'])

    def test_wrong_pid_blocks_proxy_and_guard(self):
        result, calls, _, _ = self.run_switch('start', listening=True, active=True, pid=99)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('gsettings set', calls)
        self.assertNotIn('enable --now guard.timer', calls)

    def test_direct_hot_start_failure_rolls_back_without_stopping_core(self):
        result, calls, state, config = self.run_switch(
            'start', listening=True, active=True, saved_mode='direct', controller_start_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(calls.index('enable --now controller.service'), calls.index('gsettings set'),
                        'controller 失败前不得设置系统代理；之后的写入属于回滚恢复')
        self.assertNotIn('disable --now core.service', calls)
        self.assertTrue(state['active'])
        self.assertEqual(state['runtime_mode'], 'direct')
        self.assertEqual(config['mode'], 'direct')

    def test_cold_start_failure_fully_stops_core(self):
        result, calls, state, _ = self.run_switch(
            'start', listening=False, active=False, controller_start_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('disable --now core.service', calls)
        self.assertFalse(state['active'])

    def test_own_core_before_proxy_before_guard(self):
        result, calls, _, config = self.run_switch('start', listening=True, active=True, saved_mode='direct', pid=42)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(calls.index('enable --now core.service'), calls.index('gsettings set'))
        self.assertLess(calls.index('gsettings set'), calls.index('enable --now guard.timer'))
        self.assertEqual(config['mode'], 'rule')
        self.assertTrue(config['dns']['enable'])

    def test_stop_does_not_change_foreign_proxy(self):
        result, calls, state, config = self.run_switch('stop', active=True, listening=True, host='other', port=9999)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('gsettings set', calls)
        self.assertTrue(state['active'])
        self.assertEqual(config['mode'], 'direct')

    def test_stop_cleanup_error_is_reported_and_core_stays_direct(self):
        result, calls, state, config = self.run_switch('stop', active=True, listening=True, stop_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('关闭失败', result.stderr)
        self.assertNotIn('disable --now core.service', calls)
        self.assertTrue(state['active'])
        self.assertEqual(state['runtime_mode'], 'direct')
        self.assertEqual(config['mode'], 'direct')

    def test_stop_enables_direct_core_and_is_idempotent(self):
        result, calls, state, config = self.run_switch(['stop', 'stop'], active=False, listening=False, saved_mode='direct')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(state['active'])
        self.assertTrue(state['listening'])
        self.assertEqual(state['runtime_mode'], 'direct')
        self.assertEqual(config['mode'], 'direct')
        self.assertFalse(config['dns']['enable'])
        self.assertNotIn('disable --now core.service', calls)

    def test_full_stop_cached_running_unit_even_if_file_is_missing(self):
        result, calls, state, _ = self.run_switch('full-stop', active=True, listening=True, missing_unit_file=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('systemctl --user stop core.service', calls)
        self.assertFalse(state['active'])

    def test_full_stop_releases_core_and_listeners(self):
        result, calls, state, _ = self.run_switch('full-stop', active=True, listening=True, saved_mode='direct')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('disable --now core.service', calls)
        self.assertFalse(state['active'])
        self.assertFalse(state['listening'])


if __name__ == '__main__':
    unittest.main()

"""使用隔离 HOME 与系统命令替身验证启停，不触碰真实系统服务。"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUB = '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state = json.loads(Path(os.environ['OPK_STUB_STATE']).read_text())
with open(os.environ['OPK_STUB_CALLS'], 'a') as stream:
    stream.write(name + ' ' + ' '.join(args) + '\\n')
if name == 'systemctl':
    if 'is-active' in args:
        sys.exit(0 if state.get('active') else 3)
    if 'show' in args:
        print('42')
    if 'enable' in args and state.get('start_fail'):
        sys.exit(1)
    if 'enable' in args and 'controller.service' in args and state.get('controller_start_fail'):
        sys.exit(1)
    if 'disable' in args and state.get('stop_fail'):
        sys.exit(1)
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
        Path(os.environ['OPK_STUB_STATE']).write_text(json.dumps(state))

'''


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
            envfile = root / 'kit.env'
            envfile.write_text(f"INSTALL_HOME='{root}'\nMIXED_PORT='17890'\nMIHOMO_SECRET='synthetic'\nUNIT_MIHOMO='core.service'\n"
                               "UNIT_CONTROLLER='controller.service'\nUNIT_GUARD_TIMER='guard.timer'\n"
                               "UNIT_GUARD_SERVICE='guard.service'\n")
            (root / 'state').write_text(json.dumps(state))
            env = dict(os.environ, HOME=tmp, KIT_ENV=str(envfile), PATH=str(binary) + ':' + os.environ['PATH'],
                       OPK_STUB_STATE=str(root / 'state'), OPK_STUB_CALLS=str(root / 'calls'))
            result = subprocess.run(['bash', str(ROOT / 'scripts/switch.sh'), action], env=env,
                                    capture_output=True, text=True, timeout=10)
            return result, (root / 'calls').read_text()

    def test_foreign_listener_blocks_before_mutation(self):
        result, calls = self.run_switch('start', listening=True, active=False, pid=99)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(' enable ', calls)
        self.assertNotIn('gsettings set', calls)

    def test_core_start_failure_never_sets_proxy(self):
        result, calls = self.run_switch('start', listening=False, start_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('gsettings set', calls)

    def test_wrong_pid_blocks_proxy_and_guard(self):
        result, calls = self.run_switch('start', listening=True, active=True, pid=99)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('gsettings set', calls)
        self.assertNotIn('enable --now guard.timer', calls)

    def test_route_start_failure_rolls_back_before_setting_proxy(self):
        result, calls = self.run_switch('start', listening=True, active=True, controller_start_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('gsettings set', calls)
        self.assertIn('disable --now controller.service core.service', calls)

    def test_own_core_before_proxy_before_guard(self):
        result, calls = self.run_switch('start', listening=True, active=True, pid=42)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(calls.index('enable --now core.service'), calls.index('gsettings set'))
        self.assertLess(calls.index('gsettings set'), calls.index('enable --now guard.timer'))

    def test_stop_does_not_change_foreign_proxy(self):
        result, calls = self.run_switch('stop', host='other', port=9999)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('gsettings set', calls)

    def test_stop_error_is_reported(self):
        result, calls = self.run_switch('stop', stop_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('关闭失败', result.stderr)
        self.assertNotIn('disable --now core.service', calls, '守护停止失败时不得先停核心')

    def test_stop_cached_running_unit_even_if_file_is_missing(self):
        result, calls = self.run_switch('stop', active=True, missing_unit_file=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('systemctl --user stop core.service', calls)


if __name__ == '__main__':
    unittest.main()

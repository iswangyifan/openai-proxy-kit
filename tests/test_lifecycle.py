"""在临时 HOME 中执行完整首装、关闭、移除；系统命令均为隔离替身。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from test_switch import STUB

ROOT = Path(__file__).resolve().parents[1]
FULL_STUB = STUB.replace("if name == 'systemctl':", '''if name == 'systemctl':
    if 'enable' in args and 'mihomo-kit.service' in args:
        state['active'] = True
        state['listening'] = True
        Path(os.environ['OPK_STUB_STATE']).write_text(json.dumps(state))
    if 'disable' in args and 'mihomo-kit.service' in args:
        state['active'] = False
        state['listening'] = False
        Path(os.environ['OPK_STUB_STATE']).write_text(json.dumps(state))
    if 'enable' in args and 'proxy-guard-kit.timer' in args:
        import shlex, subprocess
        units = Path.home() / '.config/systemd/user'
        timer = (units / 'proxy-guard-kit.timer').read_text()
        unit = next(line.split('=', 1)[1] for line in timer.splitlines() if line.startswith('Unit='))
        service = (units / unit).read_text()
        command = next(line.split('=', 1)[1] for line in service.splitlines() if line.startswith('ExecStart='))
        subprocess.run(shlex.split(command), check=True)
        (Path.home() / 'guard-executed').touch()
''')


class LifecycleTests(unittest.TestCase):
    def test_offline_cleanup_with_real_isolated_gsettings(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            binary = home / 'bin'; binary.mkdir()
            (binary / 'systemctl').write_text('#!/bin/sh\nexit 1\n')
            (binary / 'systemctl').chmod(0o755)
            env = dict(os.environ, HOME=tmp, XDG_CONFIG_HOME=str(home / '.config'),
                       PATH=str(binary) + ':/usr/bin:/bin', GSETTINGS_BACKEND='keyfile',
                       PYTHONPATH=str(ROOT / 'scripts'), KIT_ENV=str(home / 'kit.env'))
            code = '''
import os, subprocess
from pathlib import Path
from kit_state import private_write, quote
from system_proxy import start, settings
from package_cleanup import clean_user
home = Path.home()
env = {'INSTALL_HOME': str(home / 'data'), 'MIXED_PORT': '17890',
       'UNIT_MIHOMO': 'core.service', 'UNIT_CONTROLLER': 'controller.service',
       'UNIT_GUARD_TIMER': 'guard.timer', 'UNIT_GUARD_SERVICE': 'guard.service'}
private_write(os.environ['KIT_ENV'], ''.join(k + '=' + quote(v) + '\\n' for k,v in env.items()))
units = home / '.config/systemd/user'
units.mkdir(parents=True)
for name in [v for k,v in env.items() if k.startswith('UNIT_')]:
    (units / name).write_text('# openai-proxy-kit managed')
    wants = units / 'default.target.wants'
    wants.mkdir(exist_ok=True)
    (wants / name).symlink_to('../' + name)
start(env)
settings('set', 'http', 'host', 'corporate.invalid')
settings('set', 'http', 'port', '8888')
clean_user(keep_gui=True)
assert settings('get', 'http', 'host') == "'corporate.invalid'"
assert settings('get', 'https', 'host') == "''"
assert settings('get', '', 'mode') == "'manual'"
assert not list(units.glob('*.service'))
assert not list((units / 'default.target.wants').iterdir())
assert not Path(os.environ['KIT_ENV']).exists()
'''
            result = subprocess.run(['dbus-run-session', '--config-file=' + str(ROOT / 'tests/dbus-isolated.conf'),
                                     '--', sys.executable, '-c', code], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fresh_gnome_install_stop_and_cleanup(self):
        core = ROOT / '.build-cache/core/mihomo'
        if not core.exists():
            self.skipTest('先执行 packaging/fetch-core.sh')
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            binary = home / 'bin'; binary.mkdir()
            for name in ('systemctl', 'ss', 'gsettings', 'gnome-shell', 'loginctl', 'sleep', 'gapplication', 'systemd-analyze', 'update-desktop-database'):
                (binary / name).write_text(FULL_STUB)
                (binary / name).chmod(0o755)
            (binary / 'pgrep').write_text('#!/bin/sh\nexit 1\n')
            (binary / 'pgrep').chmod(0o755)
            (binary / 'curl').write_text('''#!/bin/sh
for url; do :; done
case "$url" in
 */proxies/*) printf '{"now":"Synthetic","all":["Synthetic"]}' ;;
 *) printf 200 ;;
esac
''')
            (binary / 'curl').chmod(0o755)
            controller = binary / 'controller'
            controller.write_text('#!/bin/sh\nexit 0\n'); controller.chmod(0o755)
            subscription = home / 'sub.yaml'
            subscription.write_text('proxies:\n- name: Synthetic\n  type: ss\n  server: 127.0.0.1\n  port: 443\n  cipher: aes-128-gcm\n  password: SYNTHETIC_ONLY\n')
            (home / 'state').write_text(json.dumps({'host': '', 'port': 0, 'mode': 'none'}))
            env = dict(os.environ, HOME=tmp, XDG_CONFIG_HOME=str(home / '.config'),
                       PATH=str(binary) + ':/usr/bin:/bin', OPK_STUB_STATE=str(home / 'state'),
                       OPK_STUB_CALLS=str(home / 'calls'), SUB_URL=str(subscription), ASSUME_YES='1',
                       MIHOMO_BIN=str(core), MIXED_PORT='17890', API_PORT='19090', DNS_PORT='1153',
                       CONTROLLER_BIN=str(controller), DESKTOP_INJECT='0')
            # 外部测试环境不得穿透到真实 KIT_ENV/安装目录。
            for key in ('KIT_ENV', 'INSTALL_HOME', 'MIHOMO_CONF_DIR', 'OPK_LOCK_HELD', 'SHADOW', 'MIHOMO_SECRET'):
                env.pop(key, None)
            installed = subprocess.run(['bash', str(ROOT / 'install.sh')], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            self.assertTrue((home / 'guard-executed').exists())
            conf = home / '.config/openai-proxy-kit/mihomo/config.yaml'
            self.assertTrue(conf.exists())
            self.assertFalse((home / '.config/mihomo').exists())
            self.assertEqual(json.loads((home / 'state').read_text())['mode'], 'manual')
            stopped = subprocess.run(['bash', str(ROOT / 'scripts/switch.sh'), 'stop'], env=env,
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
            state = json.loads((home / 'state').read_text())
            self.assertEqual(state['mode'], 'none')
            self.assertFalse(state['active'])
            cleanup = subprocess.run([sys.executable, str(ROOT / 'scripts/package_cleanup.py'), '--user'],
                                     env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
            self.assertTrue(conf.exists(), '卸载保留私有配置')
            self.assertFalse((home / '.local/share/openai-proxy-kit/kit.env').exists())
            self.assertEqual(list((home / '.config/systemd/user').glob('*kit*')), [])
            self.assertFalse((home / '.local/bin/guard-system-proxy-kit.sh').exists())


if __name__ == '__main__':
    unittest.main()

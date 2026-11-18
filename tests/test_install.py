import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ControllerInstallTests(unittest.TestCase):
    def test_route_unit_environment_path_is_accepted_by_systemd_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'path with spaces'
            root.mkdir()
            unit = root / 'opk-parser.service'
            text = (ROOT / 'templates/route-engine.service.tmpl').read_text()
            for key, value in {'UNIT_MIHOMO': 'opk-parser-core.service',
                               'CONTROLLER_ENV': str(root / 'controller.env'),
                               'CONTROLLER_BIN': str(ROOT / 'scripts/route_engine.py')}.items():
                text = text.replace('@' + key + '@', value)
            unit.write_text(text)
            (root / 'opk-parser-core.service').write_text('[Service]\nExecStart=/usr/bin/true\n')
            result = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit),
                                     str(root / 'opk-parser-core.service')],
                                    capture_output=True, text=True, timeout=10)
            # systemd 对无效 EnvironmentFile 只警告且仍返回 0，必须检查诊断。
            self.assertNotIn('EnvironmentFile= path is not absolute', result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)

    def install(self, active):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            data = home / 'data'
            data.mkdir()
            binary = home / 'bin'
            binary.mkdir()
            controller = binary / 'internal-controller'
            controller.write_text('#!/bin/sh\nexit 0\n')
            controller.chmod(0o755)
            stub = binary / 'systemctl'
            stub.write_text('''#!/usr/bin/python3
import os, sys
with open(os.environ['OPK_TEST_CALLS'], 'a') as stream:
    stream.write(' '.join(sys.argv[1:]) + '\\n')
if 'is-active' in sys.argv:
    sys.exit(0 if os.environ['OPK_TEST_ACTIVE'] == '1' else 3)
''')
            stub.chmod(0o755)
            sleep = binary / 'sleep'
            sleep.write_text('#!/bin/sh\nexit 0\n')
            sleep.chmod(0o755)
            gsettings = binary / 'gsettings'
            gsettings.write_text("#!/bin/sh\ncase \"$3\" in host) echo \"''\";; port) echo 0;; *) echo \"'none'\";; esac\n")
            gsettings.chmod(0o755)
            envfile = data / 'kit.env'
            values = {'INSTALL_HOME': str(data), 'MIXED_PORT': '17890', 'API_PORT': '19090',
                      'DNS_PORT': '1153', 'MIHOMO_SECRET': 'a' * 64, 'PROBE_PORT_BASE': '17900', 'OPENAI_GROUP': 'Test',
                      'MIHOMO_CONF_DIR': str(home / 'conf'), 'MIHOMO_BIN': '/test/mihomo',
                      'SUB_URL': 'https://example.invalid/sub', 'SHADOW': '0',
                      'UNIT_MIHOMO': 'legacy-core.service', 'UNIT_CONTROLLER': 'legacy-controller.service',
                      'UNIT_GUARD_SERVICE': 'legacy-guard.service', 'UNIT_GUARD_TIMER': 'legacy-guard.timer',
                      'CONTROLLER_BIN': str(controller), 'CONTROLLER_ENV': str(data / 'controller.env'),
                      'GUARD_LOG': str(data / 'custom-guard.log')}
            envfile.write_text(''.join(f"{key}='{value}'\n" for key, value in values.items()))
            env = dict(os.environ, HOME=str(home), INSTALL_HOME=str(data), KIT_ENV=str(envfile),
                       PATH=str(binary) + ':/usr/bin:/bin', OPK_TEST_CALLS=str(home / 'calls'),
                       OPK_TEST_ACTIVE='1' if active else '0')
            result = subprocess.run(['bash', str(ROOT / 'install.sh'), 'controller'], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            unit = (home / '.config/systemd/user/legacy-controller.service').read_text()
            self.assertIn('After=legacy-core.service', unit)
            self.assertIn('ExecStart=/usr/bin/python3 "' + str(ROOT / 'scripts/route_engine.py') + '"', unit)
            self.assertNotIn('node ', unit)
            self.assertNotIn('libexec/', unit)
            self.assertIn("GUARD_LOG='" + values['GUARD_LOG'] + "'", envfile.read_text())
            return (home / 'calls').read_text()

    def test_cold_upgrade_keeps_services_stopped(self):
        calls = self.install(False)
        self.assertNotIn(' start ', calls)
        self.assertNotIn(' restart ', calls)
        self.assertNotIn(' enable ', calls)

    def test_live_upgrade_restarts_running_components(self):
        calls = self.install(True)
        self.assertIn('restart legacy-controller.service', calls)
        self.assertIn('restart legacy-core.service', calls)


if __name__ == '__main__':
    unittest.main()

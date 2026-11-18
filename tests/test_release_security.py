"""发行安全回归：只使用临时状态、合成凭据、替身或隔离核心。"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import socket
import time
import urllib.request
import urllib.error
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import kit_state
import system_proxy
import inject_desktop
import runtime
import package_cleanup


class ReleaseSecurityTests(unittest.TestCase):
    def test_state_roundtrip_never_executes_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'kit.env'
            marker = Path(tmp) / 'executed'
            value = "https://example.invalid/sub#';touch " + str(marker) + ";#"
            kit_state.private_write(path, 'SUB_URL=' + kit_state.quote(value) + '\n')
            self.assertEqual(kit_state.load(path)['SUB_URL'], value)
            # 安全编码输出被 Bash 接收后仍只包含原值。
            assignments = subprocess.check_output([sys.executable, str(ROOT / 'scripts/kit_state.py'), 'shell', str(path)], text=True)
            subprocess.run(['bash', '-c', assignments], check=True)
            self.assertFalse(marker.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_old_malicious_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state'
            for content in ["SUB_URL='https://example.invalid/#';id;#'\n", 'PATH=/tmp\n', 'UNIT_MIHOMO=../../other.service\n']:
                path.write_text(content)
                with self.assertRaises(ValueError):
                    kit_state.load(path)

    def test_private_atomic_write_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, target = Path(tmp) / 'original', Path(tmp) / 'target'
            original.write_text('preserve')
            target.symlink_to(original)
            kit_state.private_write(target, 'private')
            self.assertEqual(original.read_text(), 'preserve')
            self.assertFalse(target.is_symlink())

    def test_uid_registry_ignores_spoofed_owners_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            uid = os.getuid()
            valid = directory / f'{uid}-valid'
            valid.write_text('synthetic')
            (directory / f'{uid + 1}-spoofed').write_text('synthetic')
            (directory / f'{uid}-symlink').symlink_to(valid)
            with patch.object(kit_state, 'REGISTRY', directory):
                self.assertEqual(kit_state.registrations(), [valid])

    def test_log_export_uses_private_file_creation(self):
        from gi.repository import Gio
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'export.log'
            target = Gio.File.new_for_path(str(path))
            target.replace_contents(b'synthetic', None, False,
                                    Gio.FileCreateFlags.REPLACE_DESTINATION | Gio.FileCreateFlags.PRIVATE, None)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_generated_config_is_private_and_authenticated(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sub = tmp / 'sub.yaml'
            sub.write_text('proxies:\n- name: synthetic\n  type: ss\n  server: 127.0.0.1\n  port: 443\n  cipher: aes-128-gcm\n  password: SYNTHETIC_ONLY\n')
            validator = tmp / 'validator'
            validator.write_text('#!/bin/sh\nstat -c %a "$5" > "' + str(tmp / 'mode') + '"\n')
            validator.chmod(0o755)
            output = tmp / 'config.yaml'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/gen_config.py'), '--out', str(output),
                                     '--mihomo-bin', str(validator)], env=dict(os.environ, SUB_URL=str(sub), MIHOMO_SECRET='a' * 64),
                                    capture_output=True, text=True, umask=0o022, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((tmp / 'mode').read_text().strip(), '600')
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            import yaml
            config = yaml.safe_load(output.read_text())
            self.assertEqual(config['secret'], 'a' * 64)
            self.assertNotIn('*', config['external-controller-cors']['allow-origins'])
            self.assertEqual(config['rules'][-1], 'MATCH,DIRECT')
            self.assertFalse(any('GEOSITE' in r or 'GEOIP' in r for r in config['rules']))
            self.assertNotIn(str(sub), result.stdout)

    def proxy_case(self, mode="'none'"):
        state = {'mode': mode, **{p: {'host': "''", 'port': '0'} for p in system_proxy.PROTOCOLS}}
        def settings(action, schema, key, value=None):
            target = state[schema] if schema else state
            if action == 'get':
                return target[key]
            target[key] = value if key == 'port' or value.startswith("'") else "'" + value + "'"
            return ''
        return state, settings

    def test_mixed_protocol_restores_only_owned_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'INSTALL_HOME': tmp, 'MIXED_PORT': '17890'}
            state, settings = self.proxy_case()
            with patch.object(system_proxy, 'settings', side_effect=settings):
                system_proxy.start(env)
                state['http'] = {'host': "'corporate.invalid'", 'port': '8888'}
                system_proxy.stop(env)
            self.assertEqual(state['http']['host'], "'corporate.invalid'")
            self.assertEqual(state['https']['host'], "''")
            self.assertEqual(state['mode'], "'manual'")

    def test_roundtrip_restores_previous_manual_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'INSTALL_HOME': tmp, 'MIXED_PORT': '17890'}
            state, settings = self.proxy_case("'manual'")
            state['http'] = {'host': "'previous.invalid'", 'port': '8080'}
            original = json.loads(json.dumps(state))
            with patch.object(system_proxy, 'settings', side_effect=settings):
                system_proxy.start(env)
                system_proxy.stop(env)
            self.assertEqual(state, original)

    def test_failed_port_write_restores_half_written_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'INSTALL_HOME': tmp, 'MIXED_PORT': '17890'}
            state, base_settings = self.proxy_case("'manual'")
            state['http'] = {'host': "'corporate.invalid'", 'port': '8080'}
            original = json.loads(json.dumps(state))
            def fail_port(action, schema, key, value=None):
                if action == 'set' and schema == 'http' and key == 'port' and value == '17890':
                    raise subprocess.CalledProcessError(1, 'synthetic gsettings failure')
                return base_settings(action, schema, key, value)
            with patch.object(system_proxy, 'settings', side_effect=fail_port):
                with self.assertRaises(subprocess.CalledProcessError):
                    system_proxy.start(env)
            self.assertEqual(state, original)
            self.assertFalse(system_proxy.snapshot_path(env).exists())

    def test_core_crash_cleanup_preserves_enabled_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'INSTALL_HOME': tmp, 'MIXED_PORT': '17890', 'UNIT_MIHOMO': 'synthetic.service'}
            state, settings = self.proxy_case()
            with patch.object(system_proxy, 'settings', side_effect=settings):
                system_proxy.start(env)
                system_proxy.stop(env, clear_intent=False)
                self.assertEqual(state['mode'], "'none'")
                self.assertTrue((Path(tmp) / 'proxy-enabled').exists())
                def command(args, **kwargs):
                    output = '42' if args[0] == 'systemctl' else 'LISTEN users:(("mihomo",pid=42,fd=7))'
                    return subprocess.CompletedProcess(args, 0, output, '')
                with patch.object(system_proxy.subprocess, 'run', side_effect=command):
                    system_proxy.resume(env)
                self.assertEqual(state['mode'], "'manual'")
                system_proxy.stop(env)
                self.assertFalse((Path(tmp) / 'proxy-enabled').exists())

    def test_interrupted_restore_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'INSTALL_HOME': tmp, 'MIXED_PORT': '17890'}
            state, base_settings = self.proxy_case("'manual'")
            state['http'] = {'host': "'corporate.invalid'", 'port': '8080'}
            original = json.loads(json.dumps(state))
            with patch.object(system_proxy, 'settings', side_effect=base_settings):
                system_proxy.start(env)
            failed = False
            def fail_once(action, schema, key, value=None):
                nonlocal failed
                if not failed and action == 'set' and schema == 'http' and key == 'port':
                    failed = True
                    raise subprocess.CalledProcessError(1, 'synthetic restore failure')
                return base_settings(action, schema, key, value)
            with patch.object(system_proxy, 'settings', side_effect=fail_once):
                with self.assertRaises(subprocess.CalledProcessError):
                    system_proxy.stop(env)
                self.assertTrue(system_proxy.snapshot_path(env).exists())
                system_proxy.stop(env)
            self.assertEqual(state, original)
            self.assertFalse(system_proxy.snapshot_path(env).exists())

    def test_package_cleanup_includes_high_uid_domain_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / '.local/share/openai-proxy-kit/kit.env'
            marker.parent.mkdir(parents=True)
            marker.write_text('synthetic')
            account = SimpleNamespace(pw_uid=200000, pw_name='domain-user', pw_dir=tmp)
            calls = []
            def command(args, **kwargs):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, '', '')
            with patch.object(package_cleanup.os, 'geteuid', return_value=0), \
                 patch.object(package_cleanup.pwd, 'getpwall', return_value=[account]), \
                 patch.object(package_cleanup, 'registrations', return_value=[]), \
                 patch.object(package_cleanup.subprocess, 'run', side_effect=command):
                package_cleanup.clean_all()
            delegated = [c for c in calls if c[0] == 'runuser']
            self.assertEqual(len(delegated), 1)
            self.assertIn('domain-user', delegated[0])
            self.assertIn('--user', delegated[0])

    def test_package_upgrade_does_not_run_removal_cleanup(self):
        result = subprocess.run(['sh', str(ROOT / 'packaging/prerm'), 'upgrade'], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_resume_does_not_take_over_foreign_manual_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'INSTALL_HOME': tmp, 'MIXED_PORT': '17890'}
            (Path(tmp) / 'proxy-enabled').write_text('enabled')
            state, settings = self.proxy_case("'manual'")
            state['http'] = {'host': "'foreign.invalid'", 'port': '8888'}
            original = json.loads(json.dumps(state))
            with patch.object(system_proxy, 'settings', side_effect=settings):
                system_proxy.resume(env)
            self.assertEqual(state, original)

    def test_legacy_https_residue_is_cleared_without_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, settings = self.proxy_case("'manual'")
            state['http'] = {'host': "'other.invalid'", 'port': '8888'}
            state['https'] = {'host': "'127.0.0.1'", 'port': '17890'}
            with patch.object(system_proxy, 'settings', side_effect=settings):
                system_proxy.stop({'INSTALL_HOME': tmp, 'MIXED_PORT': '17890'})
            self.assertEqual(state['http']['host'], "'other.invalid'")
            self.assertEqual(state['https']['port'], '0')

    def test_gui_detects_single_protocol_residue(self):
        def setting(schema, key):
            return ('127.0.0.1' if key == 'host' else runtime.MIXED_PORT) if schema.endswith('https') else ('other' if key == 'host' else '80')
        with patch.object(runtime, 'refresh_config'), \
             patch.dict(runtime.ENV, {'STATE_VERSION': '1'}, clear=True), \
             patch.object(runtime, 'unit_active', return_value=False), patch.object(runtime, 'sys_proxy_mode', return_value='manual'), \
             patch.object(runtime, 'proxy_setting', side_effect=setting), patch.object(runtime, 'mihomo_get', return_value=None), \
             patch.object(runtime, 'count_injected', return_value=0):
            self.assertEqual(runtime.query_status()['phase'], 'partial')

    def test_closed_tool_preserves_foreign_proxy_reusing_same_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'STATE_VERSION': '2', 'INSTALL_HOME': tmp, 'MIXED_PORT': '17890', 'UNIT_MIHOMO': 'own.service'}
            def command(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, '0' if args[0] == 'systemctl' else 'pid=999,', '')
            with patch.object(system_proxy.subprocess, 'run', side_effect=command), \
                 patch.object(system_proxy, 'settings') as settings:
                system_proxy.stop(env)
            settings.assert_not_called()

    def test_gui_does_not_claim_reused_foreign_proxy_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            def setting(schema, key):
                return '127.0.0.1' if key == 'host' else runtime.MIXED_PORT
            with patch.object(runtime, 'refresh_config'), \
                 patch.dict(runtime.ENV, {'STATE_VERSION': '2'}, clear=True), \
                 patch.object(runtime, 'INSTALL_HOME', Path(tmp)), \
                 patch.object(runtime, 'unit_active', return_value=False), \
                 patch.object(runtime, 'sys_proxy_mode', return_value='manual'), \
                 patch.object(runtime, 'proxy_setting', side_effect=setting), \
                 patch.object(runtime, 'mihomo_get', return_value=None), \
                 patch.object(runtime, 'count_injected', return_value=0):
                status = runtime.query_status()
            self.assertFalse(status['proxy_owned'])
            self.assertEqual(status['phase'], 'off')

    def test_existing_user_desktop_survives_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, target = root / 'system/code.desktop', root / 'apps/code.desktop'
            src.parent.mkdir(); target.parent.mkdir()
            src.write_text('[Desktop Entry]\nExec=code %F\n')
            original = '[Desktop Entry]\nName=My custom\nExec=code --my-options %F\n'
            target.write_text(original)
            backup = root / 'backup'
            manifest = {'injected': {}}
            class Args:
                proxy = 'http://127.0.0.1:17890'
            inject_desktop.do_inject(Args(), src, Args.proxy, target.parent, backup, manifest)
            self.assertIn('--my-options', target.read_text())
            injected = target.read_bytes()
            for _ in range(2):
                inject_desktop.do_enable(Args(), target.parent, backup, manifest)
                self.assertEqual(target.read_bytes(), injected)
            for _ in range(2):
                inject_desktop.do_disable(target.parent, backup, manifest)
                self.assertEqual(target.read_text(), original)
                inject_desktop.do_enable(Args(), target.parent, backup, manifest)
            inject_desktop.do_restore(target.parent, backup, manifest)
            self.assertEqual(target.read_text(), original)

    def test_guard_is_executable_and_timer_is_template(self):
        self.assertTrue((ROOT / 'templates/guard-system-proxy.sh.tmpl').read_text().startswith('#!/bin/bash\n'))
        self.assertIn('Unit=@UNIT_GUARD_SERVICE@', (ROOT / 'templates/proxy-guard.timer.tmpl').read_text())

    def test_auth_header_and_extended_redaction(self):
        with patch.dict(runtime.ENV, {'MIHOMO_SECRET': 'SYNTHETIC_ONLY'}):
            self.assertEqual(runtime.api_headers()['Authorization'], 'Bearer SYNTHETIC_ONLY')
        self.assertNotIn('SECOND', runtime.redact('Cookie: first=FIRST; second=SECOND'))
        for field in ('access_token', 'refresh_token', 'Cookie'):
            self.assertNotIn('SYNTHETIC_ONLY', runtime.redact(field + '=SYNTHETIC_ONLY'))

    def test_real_core_authentication_and_cors(self):
        binary = ROOT / '.build-cache/core/mihomo'
        if not binary.exists():
            self.skipTest('先执行 packaging/fetch-core.sh')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.yaml'
            config.write_text(f'''mixed-port: 0
external-controller: 127.0.0.1:{port}
secret: SYNTHETIC_ONLY
external-controller-cors:
  allow-origins: ["http://127.0.0.1:{port}"]
  allow-private-network: false
dns:
  enable: false
rules: [MATCH,DIRECT]
'''.replace('rules: [MATCH,DIRECT]', 'rules: ["MATCH,DIRECT"]'))
            process = subprocess.Popen([str(binary), '-d', tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def request(headers=None, method='GET'):
                req = urllib.request.Request(f'http://127.0.0.1:{port}/configs', headers=headers or {}, method=method)
                try:
                    return client.open(req, timeout=2)
                except urllib.error.HTTPError as error:
                    return error
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        response = request()
                        break
                    except OSError:
                        time.sleep(.05)
                else:
                    self.fail('隔离核心启动超时')
                with response:
                    self.assertEqual(response.code, 401)
                with request({'Authorization': 'Bearer WRONG'}) as response:
                    self.assertEqual(response.code, 401)
                with request({'Authorization': 'Bearer SYNTHETIC_ONLY'}) as response:
                    self.assertEqual(response.code, 200)
                with request({'Origin': 'https://untrusted.invalid', 'Access-Control-Request-Method': 'PATCH',
                              'Access-Control-Request-Headers': 'authorization,content-type'}, 'OPTIONS') as response:
                    self.assertNotIn(response.headers.get('Access-Control-Allow-Origin'), ('*', 'https://untrusted.invalid'))
                    self.assertNotEqual(response.headers.get('Access-Control-Allow-Private-Network'), 'true')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()

    def test_custom_core_requires_matching_origin(self):
        spec = importlib.util.spec_from_file_location('core_assets', ROOT / 'packaging/core_assets.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            binary, record = Path(tmp) / 'core', Path(tmp) / 'origin.json'
            binary.write_bytes(b'synthetic core')
            with patch.dict(os.environ, {'MIHOMO_ORIGIN': str(record)}):
                record.write_text(json.dumps({'version': 'A', 'source': 'https://example.invalid/A', 'sha256': 'wrong'}))
                with self.assertRaises(ValueError):
                    module.origin(binary)
                record.write_text(json.dumps({'version': 'B', 'source': 'https://example.invalid/B', 'sha256': module.digest(binary)}))
                with self.assertRaises(ValueError):
                    module.origin(binary)


if __name__ == '__main__':
    unittest.main()

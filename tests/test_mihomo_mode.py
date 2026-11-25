"""rule/direct 持久化、API 回读和失败回滚；只使用临时配置与命令替身。"""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import mihomo_mode


class MihomoModeTests(unittest.TestCase):
    def test_core_restarting_after_inactive_sample_is_reconciled(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            with patch.object(mihomo_mode.websites, 'validate_config'), \
                 patch.object(mihomo_mode.websites, 'active', side_effect=[False, True]), \
                 patch.object(mihomo_mode.websites, 'assert_owner'), \
                 patch.object(mihomo_mode.websites, 'api',
                              side_effect=[{'mode': 'rule'}, {}, {}, {'mode': 'direct'}]) as api:
                mihomo_mode.set_mode(env, 'direct')
            self.assertEqual(mihomo_mode.saved_mode(env), 'direct')
            self.assertIn(call(env, '/configs', {'mode': 'direct'}, method='PATCH'), api.call_args_list)

    def test_cli_sigterm_rolls_back_written_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            original = path.read_bytes()
            code = '''
import json, os, signal, sys
from unittest.mock import patch
import mihomo_mode
env = json.loads(os.environ['OPK_TEST_MODE_ENV'])
runtime_mode = ['rule']
def api(env, path, value=None, method=None):
    if method == 'PATCH':
        if value['mode'] == 'direct':
            os.kill(os.getpid(), signal.SIGTERM)
        runtime_mode[0] = value['mode']
        return {}
    return {'mode': runtime_mode[0]}
with patch.object(mihomo_mode, 'load_state', return_value=env), \\
     patch.object(mihomo_mode.websites, 'validate_config'), \\
     patch.object(mihomo_mode.websites, 'active', return_value=True), \\
     patch.object(mihomo_mode.websites, 'assert_owner'), \\
     patch.object(mihomo_mode.websites, 'api', side_effect=api), \\
     patch.object(sys, 'argv', ['mode', '--state', 'synthetic', 'set', 'direct']):
    raise SystemExit(mihomo_mode.main())
'''
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                env=dict(os.environ, PYTHONPATH=str(ROOT / 'scripts'), OPK_TEST_MODE_ENV=json.dumps(env)),
                timeout=10)
            self.assertEqual(result.returncode, 130, result.stderr)
            self.assertEqual(path.read_bytes(), original)

    def fixture(self, folder, mode='rule'):
        root = Path(folder)
        conf = root / 'conf'
        conf.mkdir()
        path = conf / 'config.yaml'
        path.write_text('# synthetic fixture\n' + yaml.safe_dump({
            'mode': mode,
            'mixed-port': 17890,
            'external-controller': '127.0.0.1:19090',
            'dns': {'enable': mode == 'rule', 'nameserver': ['https://dns.example.invalid/dns-query']},
            'rules': ['MATCH,DIRECT'],
        }))
        env = {
            'MIHOMO_CONF_DIR': str(conf),
            'MIHOMO_BIN': '/usr/bin/true',
            'UNIT_MIHOMO': 'synthetic-core.service',
            'API_PORT': '19090',
            'MIXED_PORT': '17890',
            'MIHOMO_SECRET': 'SYNTHETIC',
        }
        return env, path

    def test_saved_mode_missing_and_valid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            self.assertEqual(mihomo_mode.saved_mode(env), 'rule')
            path.write_text(yaml.safe_dump({'mode': 'direct', 'rules': []}))
            self.assertEqual(mihomo_mode.saved_mode(env), 'direct')
            path.unlink()
            self.assertIsNone(mihomo_mode.saved_mode(env))

    def test_saved_mode_rejects_missing_invalid_and_unsupported_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            for value in ({'rules': []}, {'mode': 'global', 'rules': []},
                          {'mode': True, 'rules': []}, {'mode': ['rule'], 'rules': []}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    path.write_text(yaml.safe_dump(value))
                    mihomo_mode.saved_mode(env)

    def test_stopped_set_mode_validates_and_persists_without_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            with patch.object(mihomo_mode.websites, 'active', return_value=False), \
                 patch.object(mihomo_mode.websites, 'api') as api:
                mihomo_mode.set_mode(env, 'direct')
            api.assert_not_called()
            self.assertEqual(mihomo_mode.saved_mode(env), 'direct')
            saved = yaml.safe_load(path.read_text())
            self.assertFalse(saved['dns']['enable'])
            self.assertEqual(saved['dns']['nameserver'], ['https://dns.example.invalid/dns-query'])
            self.assertTrue(path.read_text().startswith('# synthetic fixture\n'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob('.mode-*')), [])
            with patch.object(mihomo_mode.websites, 'active', return_value=False):
                mihomo_mode.set_mode(env, 'rule')
            self.assertTrue(yaml.safe_load(path.read_text())['dns']['enable'])

    def test_live_set_mode_patches_force_reloads_and_reads_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            with patch.object(mihomo_mode.websites, 'active', return_value=True), \
                 patch.object(mihomo_mode.websites, 'assert_owner') as owner, \
                 patch.object(mihomo_mode.websites, 'api',
                              side_effect=[{'mode': 'rule'}, {}, {}, {'mode': 'direct'}]) as api:
                mihomo_mode.set_mode(env, 'direct')
            self.assertEqual(owner.call_count, 2, '切换与 force reload 都要确认核心归属')
            self.assertEqual(api.call_args_list, [
                call(env, '/configs'),
                call(env, '/configs', {'mode': 'direct'}, method='PATCH'),
                call(env, '/configs?force=true', {'path': str(path.resolve())}),
                call(env, '/configs'),
            ])
            saved = yaml.safe_load(path.read_text())
            self.assertEqual(saved['mode'], 'direct')
            self.assertFalse(saved['dns']['enable'])

    def test_patch_failure_restores_disk_and_original_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            original = path.read_bytes()
            with patch.object(mihomo_mode.websites, 'active', return_value=True), \
                 patch.object(mihomo_mode.websites, 'assert_owner'), \
                 patch.object(mihomo_mode.websites, 'api',
                              side_effect=[{'mode': 'rule'}, OSError('synthetic'), {}, {}, {'mode': 'rule'}]) as api:
                with self.assertRaises(OSError):
                    mihomo_mode.set_mode(env, 'direct')
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(api.call_args_list[-3:], [
                call(env, '/configs', {'mode': 'rule'}, method='PATCH'),
                call(env, '/configs?force=true', {'path': str(path.resolve())}),
                call(env, '/configs'),
            ])

    def test_force_reload_failure_restores_and_reloads_original_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            original = path.read_bytes()
            with patch.object(mihomo_mode.websites, 'active', return_value=True), \
                 patch.object(mihomo_mode.websites, 'assert_owner'), \
                 patch.object(mihomo_mode.websites, 'api',
                              side_effect=[{'mode': 'rule'}, {}, OSError('target reload'), {}, {}, {'mode': 'rule'}]) as api:
                with self.assertRaises(OSError):
                    mihomo_mode.set_mode(env, 'direct')
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(yaml.safe_load(path.read_text())['dns']['enable'])
            self.assertEqual(api.call_args_list[-3:], [
                call(env, '/configs', {'mode': 'rule'}, method='PATCH'),
                call(env, '/configs?force=true', {'path': str(path.resolve())}),
                call(env, '/configs'),
            ])

    def test_readback_mismatch_restores_original_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            original = path.read_bytes()
            with patch.object(mihomo_mode.websites, 'active', return_value=True), \
                 patch.object(mihomo_mode.websites, 'assert_owner'), \
                 patch.object(mihomo_mode.websites, 'api',
                              side_effect=[{'mode': 'rule'}, {}, {}, {'mode': 'rule'}, {}, {}, {'mode': 'rule'}]) as api:
                with self.assertRaisesRegex(RuntimeError, '回读'):
                    mihomo_mode.set_mode(env, 'direct')
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(api.call_args_list[-3:], [
                call(env, '/configs', {'mode': 'rule'}, method='PATCH'),
                call(env, '/configs?force=true', {'path': str(path.resolve())}),
                call(env, '/configs'),
            ])

    def test_incomplete_runtime_restore_is_reported_and_disk_is_still_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            original = path.read_bytes()
            with patch.object(mihomo_mode.websites, 'active', return_value=True), \
                 patch.object(mihomo_mode.websites, 'assert_owner'), \
                 patch.object(mihomo_mode.websites, 'api',
                              side_effect=[{'mode': 'rule'}, OSError('target'), {}, OSError('rollback reload'), {'mode': 'rule'}]) as api:
                with self.assertRaisesRegex(RuntimeError, '恢复不完整'):
                    mihomo_mode.set_mode(env, 'direct')
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(api.call_count, 5, '原配置 reload 失败后仍应回读原运行模式')

    def test_validation_failure_changes_no_disk_and_touches_no_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            env['MIHOMO_BIN'] = '/usr/bin/false'
            original = path.read_bytes()
            with patch.object(mihomo_mode.websites, 'active') as active, \
                 patch.object(mihomo_mode.websites, 'api') as api:
                with self.assertRaises(ValueError):
                    mihomo_mode.set_mode(env, 'direct')
            active.assert_not_called()
            api.assert_not_called()
            self.assertEqual(path.read_bytes(), original)

    def test_unsupported_target_and_missing_config_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, path = self.fixture(tmp)
            for mode in ('global', None, ['rule']):
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    mihomo_mode.set_mode(env, mode)
            path.unlink()
            with self.assertRaises(ValueError):
                mihomo_mode.set_mode(env, 'rule')


if __name__ == '__main__':
    unittest.main()

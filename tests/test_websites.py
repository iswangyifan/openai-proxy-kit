"""代理网站的持久化、实际核心重载及失败恢复；不修改本机代理。"""
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import websites
from route_model import write_probe_map


class WebsiteTests(unittest.TestCase):
    def fixture(self, folder):
        folder = Path(folder)
        conf = folder / 'conf'
        conf.mkdir()
        value = {'mixed-port': 0, 'external-controller': '127.0.0.1:0', 'secret': 'SYNTHETIC',
                 'dns': {'enable': False},
                 'proxies': [{'name': 'A', 'type': 'http', 'server': '127.0.0.1', 'port': 9},
                             {'name': 'B', 'type': 'http', 'server': '127.0.0.1', 'port': 10}],
                 'proxy-groups': [{'name': 'Test', 'type': 'select', 'proxies': ['A', 'B']}],
                 'rules': ['DOMAIN-SUFFIX,openai.com,Test', 'DOMAIN-SUFFIX,corp.example,DIRECT', 'MATCH,DIRECT']}
        config = conf / 'config.yaml'
        config.write_text(yaml.safe_dump(value))
        write_probe_map(value, conf / 'probe-map.json')
        env = {'INSTALL_HOME': str(folder / 'data'), 'MIHOMO_CONF_DIR': str(conf), 'OPENAI_GROUP': 'Test',
               'MIHOMO_BIN': '/usr/bin/true', 'UNIT_MIHOMO': 'acceptance-core.service',
               'MIHOMO_SECRET': 'SYNTHETIC', 'API_PORT': '1', 'MIXED_PORT': '2'}
        return env, value, config

    def test_domain_normalization_and_rule_injection_rejection(self):
        self.assertEqual(websites.normalize({'domains': [' EXAMPLE.com. ', 'example.com', '例子.测试']})['domains'],
                         ['example.com', 'xn--fsqu00a.xn--0zwm56d'])
        for domain in ('http://example.com', 'example.com/path', '*.example.com', 'example.com,REJECT',
                       '127.0.0.1', '::1', 'localhost', 'work.local', 'a..com', 'a.com:80', 'com', '-bad.com'):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                websites.normalize({'domains': [domain]})

    def test_schema_and_limits(self):
        for value in ([], {'schema': True}, {'enabled': [{}]}, {'enabled': ['unknown']},
                      {'domains': ['a.com'] * 129}, {'domains': [None]}, {'extra': 'x'}):
            with self.subTest(value=type(value)), self.assertRaises(ValueError):
                websites.normalize(value)

    def test_removing_duplicate_custom_openai_domain_keeps_default_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, value, _ = self.fixture(tmp)
            old = {'enabled': [], 'domains': ['chatgpt.com']}
            value = websites.merge_rules(value, old, 'Test')
            value = websites.merge_rules(value, {'enabled': [], 'domains': []}, 'Test', old)
            for domain in websites.OPENAI_DOMAINS:
                self.assertIn('DOMAIN-SUFFIX,' + domain + ',Test', value['rules'])

    def test_invalid_yaml_does_not_expose_source_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.yaml'
            path.write_text('password: SYNTHETIC_PRIVATE: invalid\n')
            with self.assertRaises(ValueError) as error:
                websites.read_config(path)
            self.assertNotIn('SYNTHETIC_PRIVATE', str(error.exception))

    def test_managed_rules_remove_old_domains_preserve_company_direct_and_never_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, value, _ = self.fixture(tmp)
            old = {'enabled': ['github'], 'domains': ['old.example']}
            first = websites.merge_rules(copy.deepcopy(value), old, 'Test')
            second = websites.merge_rules(first, {'enabled': ['youtube'], 'domains': ['new.example']}, 'Test', old)
            self.assertNotIn('DOMAIN-SUFFIX,github.com,Test', second['rules'])
            self.assertNotIn('DOMAIN-SUFFIX,old.example,Test', second['rules'])
            self.assertIn('DOMAIN-SUFFIX,googlevideo.com,Test', second['rules'])
            self.assertIn('DOMAIN-SUFFIX,new.example,Test', second['rules'])
            self.assertIn('DOMAIN-SUFFIX,corp.example,DIRECT', second['rules'])
            self.assertIn('DOMAIN-SUFFIX,openai.com,Test', second['rules'])
            self.assertEqual(second['rules'][:len(websites.PRIVATE_RULES)], websites.PRIVATE_RULES)
            self.assertEqual(second['rules'][-1], 'MATCH,DIRECT')

    def test_before_configuration_saves_private_preferences_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'INSTALL_HOME': tmp}
            self.assertEqual(websites.apply_profile(env, {'enabled': ['github']}), {'configured': False, 'live': False})
            path = websites.location(env)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(websites.load_profile(path)['enabled'], ['github'])

    def test_stopped_apply_does_not_start_service_and_updates_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _, config = self.fixture(tmp)
            with patch.object(websites, 'active', return_value=False), patch.object(websites, 'reload_core') as reload:
                result = websites.apply_profile(env, {'enabled': ['github'], 'domains': ['extra.example']})
            self.assertFalse(result['live'])
            reload.assert_not_called()
            self.assertIn('DOMAIN-SUFFIX,github.com,Test', yaml.safe_load(config.read_text())['rules'])
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            mapping = config.parent / 'probe-map.json'
            expected = config.parent / 'expected.json'
            write_probe_map(yaml.safe_load(config.read_text()), expected)
            self.assertEqual(mapping.read_bytes(), expected.read_bytes())

    def test_validation_failure_keeps_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _, config = self.fixture(tmp)
            env['MIHOMO_BIN'] = '/usr/bin/false'
            original, mapping = config.read_bytes(), (config.parent / 'probe-map.json').read_bytes()
            with self.assertRaises(ValueError):
                websites.apply_profile(env, {'enabled': ['github']})
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual((config.parent / 'probe-map.json').read_bytes(), mapping)
            self.assertFalse(websites.location(env).exists())

    def test_reload_failure_rolls_back_config_map_and_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _, config = self.fixture(tmp)
            prefs = websites.location(env)
            websites.private_write(prefs, json.dumps({'enabled': [], 'domains': ['prior.example']}))
            files = [config, config.parent / 'probe-map.json', prefs]
            before = [p.read_bytes() for p in files]
            with patch.object(websites, 'active', return_value=True), patch.object(websites, 'assert_owner'), \
                 patch.object(websites, 'api', return_value={}), \
                 patch.object(websites, 'reload_core', side_effect=[OSError('lost response'), None]) as reload:
                with self.assertRaises(OSError):
                    websites.apply_profile(env, {'enabled': ['github']})
                self.assertEqual(reload.call_count, 2)
            self.assertEqual([p.read_bytes() for p in files], before)
            self.assertFalse(prefs.with_name('website-routing.pending.json').exists())

    def test_unfinished_transaction_is_restored_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _, config = self.fixture(tmp)
            prefs = websites.location(env)
            journal = prefs.with_name('website-routing.pending.json')
            record = {'config': config.read_text(), 'map': (config.parent / 'probe-map.json').read_text(),
                      'prefs': None, 'live': False}
            websites.private_write(journal, json.dumps(record))
            config.write_text('invalid config from interrupted operation')
            with patch.object(websites, 'active', return_value=False):
                websites.apply_profile(env, {'enabled': ['youtube']})
            self.assertIn('DOMAIN-SUFFIX,googlevideo.com,Test', yaml.safe_load(config.read_text())['rules'])
            self.assertFalse(journal.exists())

    def test_foreign_port_rejected_before_files_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _, config = self.fixture(tmp)
            before = config.read_bytes()
            with patch.object(websites, 'active', return_value=True), \
                 patch.object(websites, 'assert_owner', side_effect=RuntimeError('foreign port')):
                with self.assertRaises(RuntimeError):
                    websites.apply_profile(env, {'enabled': ['github']})
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse(websites.location(env).exists())

    def test_generation_and_upgrade_preserve_saved_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, value, config = self.fixture(tmp)
            prefs = websites.location(env)
            websites.private_write(prefs, json.dumps({'enabled': ['github'], 'domains': ['extra.example']}))
            sub = Path(tmp) / 'subscription.yaml'
            sub.write_text(yaml.safe_dump({'proxies': value['proxies']}))
            generated = config.parent / 'new.yaml'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/gen_config.py'), '--sub-url', str(sub),
                '--out', str(generated), '--openai-group', 'Test', '--website-settings', str(prefs)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('DOMAIN-SUFFIX,github.com,Test', yaml.safe_load(generated.read_text())['rules'])
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/secure_config.py'), str(generated)],
                env=dict(os.environ, INSTALL_HOME=env['INSTALL_HOME'], OPENAI_GROUP='Test', MIHOMO_SECRET='SYNTHETIC_NEW'),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('DOMAIN-SUFFIX,extra.example,Test', yaml.safe_load(generated.read_text())['rules'])

    def test_cli_rejects_concurrent_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            import fcntl
            state = Path(tmp) / 'kit.env'
            with open(str(state) + '.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/websites.py'), '--state', str(state), '--apply'],
                    input='{"enabled":["github"]}', capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('另一个', result.stderr)

    def test_preconfiguration_cli_respects_explicit_state_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'custom/kit.env'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/websites.py'), '--state', str(state), '--apply'],
                input='{"enabled":["github"]}', capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((state.parent / 'website-routing.json').is_file())

    def test_public_probes_are_semantic_read_only_and_discard_node_changes(self):
        from route_model import Evidence
        with tempfile.TemporaryDirectory() as tmp:
            env, _, _ = self.fixture(tmp)
            def curl(args, **kwargs):
                self.assertEqual(args[1], '-q', '公开检测不得读取个人curlrc中的Cookie或额外请求')
                self.assertEqual(args[args.index('--proxy')+1], 'http://127.0.0.1:2')
                self.assertEqual(args[args.index('--proto')+1], '=https')
                body = Path(args[args.index('--output')+1])
                body.write_text('{"ssh_key_fingerprints":{}}' if 'api.github.com' in args[-1] else 'User-agent: *\n')
                return subprocess.CompletedProcess(args, 0, '200', '')
            with patch.object(websites, 'assert_owner'), patch.object(websites, 'api', return_value={'now':'A'}) as api, \
                 patch.object(websites.subprocess, 'run', side_effect=curl), \
                 patch('route_engine.CurlProbe', return_value=lambda _: Evidence(True, 1)):
                result = websites.probe_services(env, {'enabled':['github','youtube']})
                self.assertTrue(all(row['ok'] for row in result.values()))
                self.assertTrue(all(len(call.args)==2 for call in api.call_args_list), '检测不能写入API')
            with patch.object(websites, 'assert_owner'), patch.object(websites, 'api', side_effect=[{'now':'A'},{'now':'B'}]), \
                 patch('route_engine.CurlProbe', return_value=lambda _: Evidence(True, 1)):
                with self.assertRaisesRegex(RuntimeError, '节点或规则'):
                    websites.probe_services(env, {'enabled':[]})

    def test_real_core_hot_reload_rules_and_current_selection(self):
        binary = ROOT / '.build-cache/core/mihomo'
        if not binary.exists():
            self.skipTest('需要固定核心缓存')
        def port():
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                return listener.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            env, value, config = self.fixture(tmp)
            mixed, controller = port(), port()
            value.update({'mixed-port': mixed, 'external-controller': '127.0.0.1:' + str(controller)})
            config.write_text(yaml.safe_dump(value))
            env.update(MIHOMO_BIN=str(binary), MIXED_PORT=str(mixed), API_PORT=str(controller))
            process = subprocess.Popen([str(binary), '-d', str(config.parent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            original_run = subprocess.run
            def commands(args, **kwargs):
                if args[0] == 'systemctl':
                    return subprocess.CompletedProcess(args, 0, str(process.pid) + '\n', '')
                return original_run(args, **kwargs)
            try:
                for _ in range(40):
                    try:
                        websites.api(env, '/version')
                        break
                    except OSError:
                        time.sleep(.1)
                websites.api(env, '/proxies/Test', {'name': 'B'})
                with patch.object(websites.subprocess, 'run', side_effect=commands):
                    result = websites.apply_profile(env, {'enabled': ['github', 'youtube'], 'domains': ['extra.example']})
                self.assertTrue(result['live'])
                self.assertIsNone(process.poll(), '热重载不能重启核心进程')
                rules = websites.api(env, '/rules')['rules']
                self.assertTrue(any(r.get('payload') == 'github.com' and r.get('proxy') == 'Test' for r in rules))
                self.assertTrue(any(r.get('payload') == 'googlevideo.com' for r in rules))
                self.assertEqual(websites.api(env, '/proxies/Test')['now'], 'B')
                with patch.object(websites.subprocess, 'run', side_effect=commands):
                    websites.apply_profile(env, {'enabled': [], 'domains': []})
                self.assertFalse(any(r.get('payload') == 'github.com' for r in websites.api(env, '/rules')['rules']))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()


if __name__ == '__main__':
    unittest.main()

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from node_checks import NodeChecker, check_context, node_rows
from route_model import Evidence, RouteChanged


class NodeChecksTests(unittest.TestCase):
    def test_only_fresh_success_is_shown_and_sorted(self):
        batch = {'generation': 'g', 'results': {
            'slow': {'ok': True, 'milliseconds': 80, 'checked_at': 900},
            'fast': {'ok': True, 'milliseconds': 20, 'checked_at': 900},
            'failed': {'ok': False, 'milliseconds': 0, 'checked_at': 900},
            'old': {'ok': True, 'milliseconds': 1, 'checked_at': 700},
            'entry': {'ok': None, 'milliseconds': 0, 'checked_at': 900}}}
        names = ['slow', 'failed', 'unknown', 'old', 'fast', 'entry']
        rows = node_rows(names, batch, 'g', now=1000)
        self.assertEqual([r['name'] for r in rows], ['fast', 'slow'])
        states = {r['name']: r['state'] for r in node_rows(names, batch, 'g', True, now=1000)}
        self.assertEqual(states['failed'], '检测失败')
        self.assertEqual(states['unknown'], '未检测')
        self.assertEqual(states['entry'], '检测入口不可用')
        self.assertEqual(states['old'], '已过期，请重测')
        for generation in ('changed', None):
            self.assertEqual(node_rows(names, batch, generation, now=1000), [])
        self.assertEqual(node_rows(['slow'], batch, 'g', now=899), [])

    def checker(self):
        checker = NodeChecker({'PROBE_PORT_BASE': '18000'})
        checker.api = Mock()
        checker.api.snapshot.return_value = ('A', ('A', 'B', 'C'))
        checker.context = Mock(return_value='generation')
        checker.api.probe_port.side_effect = lambda name, pool, base: base + pool.index(name)
        return checker

    def test_probes_independent_ports_without_switching(self):
        checker = self.checker()

        def probe(proxy):
            if proxy.endswith('18002'):
                raise OSError('entry unavailable')
            return Evidence(proxy.endswith('18000'), 12)

        with patch('node_checks.CurlProbe', return_value=probe):
            result = checker.run()
        self.assertTrue(result['results']['A']['ok'])
        self.assertFalse(result['results']['B']['ok'])
        self.assertIsNone(result['results']['C']['ok'])
        self.assertEqual(result['results']['A']['milliseconds'], 12)
        checker.api.select.assert_not_called()
        checker.api.request.assert_not_called()

    def test_entry_mapping_error_is_not_a_node_failure(self):
        checker = self.checker()
        checker.api.probe_port.side_effect = RouteChanged('mapping mismatch')
        with patch('node_checks.CurlProbe') as probe:
            result = checker.run()
        self.assertTrue(all(r['ok'] is None for r in result['results'].values()))
        probe.assert_not_called()

    def test_changed_core_or_pool_discards_whole_batch(self):
        for change_pool in (False, True):
            checker = self.checker()
            if change_pool:
                checker.api.snapshot.side_effect = [('A', ('A', 'B', 'C')), ('A', ('A', 'B'))]
            else:
                checker.context.side_effect = ['before', 'after']
            with patch('node_checks.CurlProbe', return_value=lambda _: Evidence(True, 10)):
                with self.assertRaises(RouteChanged):
                    checker.run()

    def test_same_name_changed_config_invalidates_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config, mapping = directory / 'config.yaml', directory / 'probe-map.json'
            config.write_text('synthetic config A')
            mapping.write_text('synthetic map A')
            checker = NodeChecker({'MIHOMO_CONF_DIR': tmp})
            with patch.object(checker.api, 'signature', return_value='signature'):
                before = checker.context()
                config.write_text('synthetic config B')
                self.assertNotEqual(before, checker.context())
                before = checker.context()
                mapping.write_text('synthetic map B')
                self.assertNotEqual(before, checker.context())

    def test_missing_context_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(check_context({'MIHOMO_CONF_DIR': tmp}))


if __name__ == '__main__':
    unittest.main()

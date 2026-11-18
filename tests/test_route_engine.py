"""本项目路由引擎的状态转换与选择事务，不连接真实服务。"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from route_engine import Api, CurlProbe, Engine, JsonLog, load_ledger
from route_model import Evidence, Ledger, RouteChanged, ordered_nodes, write_probe_map


class FakeApi:
    def __init__(self):
        self.now, self.pool, self.epoch = 'A', ('A', 'B', 'C'), 'generation-1'
        self.writes, self.connections = [], []
        self.on_write = None
        self.missing_listener = False

    def snapshot(self):
        return self.now, self.pool

    def signature(self):
        return self.epoch

    def probe_port(self, name, pool, base):
        if self.missing_listener:
            raise RouteChanged('probe missing')
        return base + pool.index(name)

    def select(self, name):
        self.writes.append(name)
        self.now = name
        if self.on_write:
            self.on_write(name)

    def request(self, path):
        return {'connections': self.connections}


class Time:
    def __init__(self):
        self.now = 1000.0

    def clock(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds
        return False


class RouteTests(unittest.TestCase):
    def engine(self, probe=None, shadow=False):
        api, clock, events = FakeApi(), Time(), []
        engine = Engine(api, probe or (lambda _: Evidence(True, 10)), {},
                        lambda event, **fields: events.append((event, fields)),
                        'http://127.0.0.1:7890', clock=clock.clock, monotonic=clock.clock,
                        wait=clock.wait, shadow=shadow)
        engine.epoch = api.signature()
        return engine, api, clock, events

    def test_real_core_null_connections_does_not_abort_route_cycle(self):
        engine, api, clock, _ = self.engine()
        api.connections = None
        engine.step()
        self.assertFalse(engine.last_traffic)
        self.assertEqual(engine.ledger.node('B')['checks'], 1)

    def test_cancel_during_qualification_does_not_punish(self):
        engine, api, _, _ = self.engine()
        engine.wait = lambda _: True
        with self.assertRaises(RouteChanged):
            engine.recover('A', api.pool)
        self.assertEqual(engine.ledger.node('B')['level'], 0)
        self.assertEqual(api.writes, [])

    def test_probe_over_budget_does_not_select(self):
        engine, api, clock, _ = self.engine()
        def probe(_):
            clock.now += 31
            return Evidence(True, 10)
        engine.probe = probe
        with self.assertRaises(RouteChanged):
            engine.recover('A', api.pool)
        self.assertEqual(api.writes, [])

    def test_listener_disappearing_discards_failed_evidence(self):
        engine, api, _, _ = self.engine()
        def probe(_):
            api.missing_listener = True
            return Evidence(False, 10)
        engine.probe = probe
        with self.assertRaises(RouteChanged):
            engine.recover('A', api.pool)
        self.assertEqual(engine.ledger.node('B')['checks'], 0)
        self.assertEqual(engine.ledger.node('B')['level'], 0)

    def test_active_hot_standbys_mature_with_probe_cost(self):
        engine, api, clock, _ = self.engine()
        api.pool = ('A', 'B', 'C', 'D')
        api.connections = [{'metadata': {'host': 'chatgpt.com'}}]
        def probe(_):
            clock.now += 1
            return Evidence(True, 10)
        engine.probe = probe
        for _ in range(15):
            engine.step()
            clock.now += 30
        self.assertTrue(all(engine.ledger.ready(n, clock.now) for n in engine.hot_nodes))
        self.assertGreater(engine.ledger.node('D')['checks'], 0)

    def test_node_sort_matches_manifest_with_astral_characters(self):
        self.assertEqual(ordered_nodes(['Ａ', '😀', 'A', 'A']), ['A', '😀', 'Ａ'])

    def test_complete_switch_uses_candidate_then_formal_entry(self):
        calls = []
        engine, api, _, events = self.engine(lambda proxy: (calls.append(proxy), Evidence(True, 10))[1])
        persisted = []
        engine.save = lambda: persisted.append(copy.deepcopy(engine.ledger.data))
        engine.recover('A', api.pool)
        self.assertEqual(api.now, 'B')
        self.assertEqual(api.writes, ['B'])
        self.assertEqual(calls.count('http://127.0.0.1:17901'), 4)
        self.assertEqual(calls.count('http://127.0.0.1:7890'), 3)
        self.assertEqual(persisted[0]['pending']['from'], 'A')
        self.assertIsNone(engine.ledger.data['pending'])
        self.assertIn('route_committed', [row[0] for row in events])

    def test_failed_formal_path_rolls_back_and_isolates(self):
        engine, api, clock, _ = self.engine(lambda proxy: Evidence(not proxy.endswith(':7890')))
        self.assertFalse(engine.commit('B', 'A', api.pool, clock.now + 90))
        self.assertEqual(api.writes, ['B', 'A'])
        self.assertGreater(engine.ledger.node('B')['until'], clock.now)
        self.assertIsNone(engine.ledger.data['pending'])

    def test_manual_change_during_qualification_discards_evidence(self):
        engine, api, _, _ = self.engine()
        def probe(_):
            api.now = 'C'
            return Evidence(True)
        engine.probe = probe
        with self.assertRaises(RouteChanged):
            engine.node_probe('B', 'A', api.pool)
        self.assertEqual(api.writes, [])
        self.assertEqual(engine.ledger.node('B')['checks'], 0)

    def test_manual_change_after_write_is_never_overwritten(self):
        engine, api, clock, _ = self.engine()
        api.on_write = lambda name: setattr(api, 'now', 'C')
        with self.assertRaises(RouteChanged):
            engine.commit('B', 'A', api.pool, clock.now + 90)
        self.assertEqual(api.now, 'C')
        self.assertEqual(api.writes, ['B'])

    def test_generation_change_does_not_penalize_candidate(self):
        engine, api, _, _ = self.engine()
        def probe(_):
            api.epoch = 'generation-2'
            return Evidence(False)
        engine.probe = probe
        with self.assertRaises(RouteChanged):
            engine.node_probe('B', 'A', api.pool)
        self.assertEqual(engine.ledger.node('B')['level'], 0)
        self.assertEqual(api.writes, [])

    def test_missing_probe_listener_does_not_isolate_node(self):
        engine, api, _, _ = self.engine()
        api.missing_listener = True
        with self.assertRaises(RouteChanged):
            engine.recover('A', api.pool)
        self.assertEqual(engine.ledger.node('B')['level'], 0)

    def test_deadline_is_not_counted_as_bad_node(self):
        engine, api, clock, _ = self.engine()
        with self.assertRaises(RouteChanged):
            engine.qualify('B', 'A', api.pool, 3, clock.now - 1)
        self.assertEqual(engine.ledger.node('B')['checks'], 0)

    def test_pending_transaction_is_recovered_after_restart(self):
        engine, api, _, _ = self.engine()
        api.now = 'B'
        engine.ledger.data['pending'] = {'from': 'A', 'to': 'B', 'pool': list(api.pool), 'epoch': api.epoch}
        engine.rollback()
        self.assertEqual(api.writes, ['A'])

    def test_pending_transaction_does_not_cross_core_generation(self):
        engine, api, _, _ = self.engine()
        api.now = 'B'
        engine.ledger.data['pending'] = {'from': 'A', 'to': 'B', 'pool': list(api.pool), 'epoch': 'older'}
        engine.rollback()
        self.assertEqual(api.writes, [])
        self.assertEqual(api.now, 'B')

    def test_failed_api_write_is_reconciled_even_if_it_took_effect(self):
        engine, api, clock, _ = self.engine()
        def fail_once(name):
            api.on_write = None
            raise OSError('synthetic response lost')
        api.on_write = fail_once
        with self.assertRaises(OSError):
            engine.commit('B', 'A', api.pool, clock.now + 90)
        self.assertEqual(api.writes, ['B', 'A'])

    def test_state_write_failure_prevents_selector_mutation(self):
        engine, api, clock, _ = self.engine()
        def fail_save():
            raise OSError('synthetic disk full')
        engine.save = fail_save
        with self.assertRaises(OSError):
            engine.commit('B', 'A', api.pool, clock.now + 90)
        self.assertEqual(api.writes, [])

    def test_cancel_after_selection_rolls_back(self):
        engine, api, clock, _ = self.engine()
        api.on_write = lambda _: engine.cancel.set()
        with self.assertRaises(RouteChanged):
            engine.commit('B', 'A', api.pool, clock.now + 90)
        self.assertEqual(api.writes, ['B', 'A'])

    def test_transient_failure_does_not_switch(self):
        engine, api, clock, _ = self.engine(lambda _: Evidence(False))
        engine.step()
        clock.now += 20
        engine.step()
        self.assertEqual(api.writes, [])

    def test_sustained_failure_triggers_qualified_recovery(self):
        engine, api, clock, _ = self.engine()
        engine.next_scan = 99999
        def probe(proxy):
            return Evidence(not (proxy.endswith(':7890') and api.now == 'A'))
        engine.probe = probe
        for _ in range(3):
            engine.step(); clock.now += 20
        self.assertEqual(api.now, 'B')

    def test_shadow_never_mutates_selection_or_saves(self):
        engine, api, clock, _ = self.engine(lambda _: Evidence(False), shadow=True)
        engine.save = lambda: self.fail('shadow must not save')
        for _ in range(5):
            engine.step(); clock.now += 30
        self.assertEqual(api.writes, [])

    def test_hot_reserve_requires_time_span_not_one_burst(self):
        ledger = Ledger()
        for now in (1000, 1000.25, 1000.5):
            ledger.record('B', Evidence(True), now)
        self.assertFalse(ledger.ready('B', 1001))
        ledger.record('B', Evidence(True), 1060)
        self.assertTrue(ledger.ready('B', 1060))
        ledger.record('B', Evidence(True), 1300)
        self.assertFalse(ledger.ready('B', 1300))

    def test_isolation_is_bounded_and_not_extended_in_same_period(self):
        ledger = Ledger()
        ledger.isolate('B', 1000)
        first = ledger.node('B')['until']
        ledger.isolate('B', 1001)
        self.assertEqual(ledger.node('B')['until'], first)
        ledger.isolate('B', first + 1)
        self.assertEqual(ledger.node('B')['level'], 2)

    def test_unobserved_gap_does_not_reduce_penalty(self):
        ledger = Ledger()
        ledger.node('B')['level'] = 2
        ledger.record('B', Evidence(True), 1000)
        ledger.record('B', Evidence(True), 3000)
        ledger.record('B', Evidence(True), 3010)
        self.assertEqual(ledger.node('B')['level'], 2)

    def test_active_usage_keeps_cold_exploration(self):
        engine, api, clock, _ = self.engine()
        api.pool = ('A', 'B', 'C', 'D', 'E')
        api.connections = [{'metadata': {'host': 'chatgpt.com'}}]
        calls = []
        engine.probe = lambda proxy: (calls.append(proxy), Evidence(True))[1]
        for _ in range(6):
            engine.step(); clock.now += 30
        self.assertIn('http://127.0.0.1:17903', calls)

    def test_false_domain_substring_does_not_count_as_active(self):
        engine, api, _, _ = self.engine()
        api.connections = [{'metadata': {'host': 'chatgpt.com.attacker.invalid'}}]
        engine.observe_traffic()
        self.assertEqual(engine.last_traffic, 0)

    def test_map_is_private_and_tied_to_full_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'map.json'
            config = {'listeners': [{'type': 'mixed', 'listen': '127.0.0.1', 'port': 17900, 'proxy': 'A'}], 'proxies': [{'name': 'A', 'password': 'first'}]}
            write_probe_map(config, path)
            old = json.loads(path.read_text())
            config['proxies'][0]['password'] = 'second'
            write_probe_map(config, path)
            self.assertNotEqual(old['generation'], json.loads(path.read_text())['generation'])
            self.assertNotIn('second', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_malformed_state_is_rejected_before_network_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state'
            path.write_text('{"schema":1,"nodes":{"A":{"checks":"bad"}}}')
            with self.assertRaises(ValueError):
                load_ledger(path)

    def test_api_refuses_non_loopback_control_address(self):
        with self.assertRaises(ValueError):
            Api('http://example.invalid:9090', '', 'Test')

    def test_probe_requires_success_status_and_semantic_body(self):
        calls = []
        def run(args, **kwargs):
            body = Path(args[args.index('--output') + 1])
            body.write_bytes(b'ip=127.0.0.1\nloc=ZZ\n' if 'trace' in args[-1] else b'User-agent: *\n')
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, '200', '')
        with patch('route_engine.subprocess.run', side_effect=run):
            self.assertTrue(CurlProbe()('http://127.0.0.1:1').ok)
        self.assertEqual(len(calls), 2)
        def challenge(args, **kwargs):
            Path(args[args.index('--output') + 1]).write_text('captcha')
            return subprocess.CompletedProcess(args, 0, '200', '')
        with patch('route_engine.subprocess.run', side_effect=challenge):
            self.assertFalse(CurlProbe()('http://127.0.0.1:1').ok)

    def test_node_urls_and_api_key_are_redacted(self):
        lines = []
        with patch('builtins.print', side_effect=lambda line, **_: lines.append(line)), patch.dict(os.environ, {'MIHOMO_SECRET': 'TEST_KEY'}):
            JsonLog()('test', node='https://example.invalid/token', detail='TEST_KEY')
        self.assertNotIn('example.invalid', lines[0])
        self.assertNotIn('TEST_KEY', lines[0])
        json.loads(lines[0])


if __name__ == '__main__':
    unittest.main()

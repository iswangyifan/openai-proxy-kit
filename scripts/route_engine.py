#!/usr/bin/python3
"""本项目的 Linux 路由引擎：独立证据、受保护的选择事务与恢复日志。"""
import argparse
import fcntl
import hashlib
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from kit_state import private_write
from route_model import Evidence, Ledger, RouteChanged, ordered_nodes

ENDPOINTS = ('https://chatgpt.com/cdn-cgi/trace', 'https://chatgpt.com/robots.txt')


class JsonLog:
    def __init__(self, path=''):
        self.file = None
        if path:
            try:
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(target, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
                self.file = RotatingFileHandler(target, maxBytes=2 * 1024 * 1024, backupCount=3)
            except OSError:
                print('{"event":"log_file_unavailable","level":"warning"}', flush=True)

    def __call__(self, event, **fields):
        # 不记录响应体、订阅、代理端点或认证信息。节点显示名仍需人工脱敏后分享。
        def clean(value):
            if isinstance(value, str):
                value = re.sub(r'(?i)\b(?:https?|ss|ssr|vmess|vless|trojan|hysteria2)://[^\s]+', '[URL hidden]', value)
                secret = os.environ.get('MIHOMO_SECRET', '')
                return value.replace(secret, '[secret hidden]') if secret else value
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value
        row = {'time': time.time(), 'event': event, **{key: clean(value) for key, value in fields.items()}}
        line = json.dumps(row, ensure_ascii=False)
        print(line, flush=True)
        if self.file:
            try:
                self.file.emit(logging.makeLogRecord({'msg': line, 'levelno': logging.INFO, 'levelname': 'INFO'}))
            except OSError:
                pass


class Api:
    def __init__(self, address, secret, group, mapping=None, unit=''):
        parsed = urllib.parse.urlsplit(address)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1') or parsed.username or parsed.path not in ('', '/'):
            raise ValueError('管理接口必须为回环 HTTP 地址')
        self.base = address.rstrip('/')
        self.secret = secret
        self.group = group
        self.mapping, self.unit = Path(mapping) if mapping else None, unit
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, value=None):
        headers = {'Authorization': 'Bearer ' + self.secret} if self.secret else {}
        if value is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path, data=json.dumps(value).encode() if value is not None else None,
                                         method='PUT' if value is not None else 'GET', headers=headers)
        with self.client.open(request, timeout=4) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                raise ValueError('管理接口响应过大')
            return json.loads(payload) if payload else {}

    def snapshot(self):
        value = self.request('/proxies/' + urllib.parse.quote(self.group, safe=''))
        if not isinstance(value, dict):
            raise ValueError('管理接口返回格式无效')
        names = value.get('all', [])
        if value.get('type') not in (None, 'Selector') or not isinstance(names, list) or not names or len(names) > 256:
            raise ValueError('代理组必须为非空 Selector，最多 256 个候选')
        nodes = ordered_nodes(names)
        if value.get('now') not in nodes:
            raise ValueError('当前选择必须为组内节点')
        return value['now'], tuple(nodes)

    def mapping_data(self):
        if self.mapping is None or not self.mapping.is_file() or self.mapping.stat().st_size > 131072:
            raise RouteChanged('缺少本项目探测映射，请重新配置')
        value = json.loads(self.mapping.read_text())
        if not isinstance(value, dict) or not isinstance(value.get('bindings'), dict) or not isinstance(value.get('generation'), str) or not re.fullmatch('[0-9a-f]{64}', value['generation']):
            raise RouteChanged('探测映射格式无效')
        return value

    def pid(self):
        if not self.unit:
            return ''
        result = subprocess.run(['systemctl', '--user', 'show', self.unit, '-p', 'MainPID', '--value'],
                                capture_output=True, text=True, timeout=5)
        pid = result.stdout.strip()
        if result.returncode or not pid.isdigit() or int(pid) <= 0:
            raise RouteChanged('核心尚未就绪')
        return pid

    def signature(self):
        return self.mapping_data()['generation'] + ':' + self.pid()

    def probe_port(self, name, pool, base):
        port = base + pool.index(name)
        binding = self.mapping_data()['bindings'].get(name)
        if type(binding) is not int or binding != port:
            raise RouteChanged('探测映射与当前代理组不一致')
        pid = self.pid()
        if pid:
            listeners = subprocess.run(['ss', '-tlnpH', 'sport = :' + str(port)], capture_output=True,
                                       text=True, timeout=5).stdout
            if f'pid={pid},' not in listeners:
                raise RouteChanged('探测端口不属于本工具核心')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.5):
                pass
        except OSError as exc:
            raise RouteChanged('探测入口不可达') from exc
        return port

    def select(self, name):
        self.request('/proxies/' + urllib.parse.quote(self.group, safe=''), {'name': name})


class CurlProbe:
    def __init__(self, executable='/usr/bin/curl', timeout=8, endpoints=ENDPOINTS):
        self.executable, self.timeout, self.endpoints = executable, timeout, endpoints

    def __call__(self, proxy):
        started = time.monotonic()
        for index, endpoint in enumerate(self.endpoints):
            with tempfile.TemporaryDirectory(prefix='opk-probe-') as temporary:
                body = Path(temporary) / 'body'
                command = [self.executable, '--silent', '--show-error', '--proxy', proxy, '--noproxy', '',
                           '--proto', '=https', '--connect-timeout', '4', '--max-time', str(self.timeout),
                           '--max-filesize', '524288', '--output', str(body), '--write-out', '%{http_code}', endpoint]
                try:
                    result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout + 2)
                except (OSError, subprocess.TimeoutExpired):
                    return Evidence(False, reason='transport_error')
                if result.returncode or result.stdout.strip() != '200' or not body.is_file():
                    return Evidence(False, reason='incomplete_or_http_error')
                content = body.read_bytes()
                if not content or len(content) > 524288:
                    return Evidence(False, reason='invalid_body')
                if index == 0 and not (b'ip=' in content and b'loc=' in content):
                    return Evidence(False, reason='unexpected_trace')
                if index == 1 and b'user-agent:' not in content.lower():
                    return Evidence(False, reason='unexpected_robots')
        return Evidence(True, (time.monotonic() - started) * 1000)


class Engine:
    def __init__(self, api, probe, state, log, proxy, probe_base=17900, shadow=False,
                 clock=time.time, monotonic=time.monotonic, wait=None):
        self.api, self.probe, self.ledger, self.log = api, probe, Ledger(state), log
        self.proxy, self.probe_base, self.shadow = proxy, probe_base, shadow
        self.clock, self.monotonic = clock, monotonic
        self.cancel = threading.Event()
        self.wait = wait or self.cancel.wait
        self.save = lambda: None
        self.fail_streak = 0
        self.next_recovery = 0
        self.recovery_level = 0
        self.next_scan = 0
        self.failure_started = 0
        self.last_traffic = 0
        self.epoch = None
        self.last_current_ok = False
        self.scan_turn = 0
        self.hot_turn = 0
        self.hot_nodes = []

    def guard(self, expected, pool):
        if self.cancel.is_set() or self.api.snapshot() != (expected, pool) or self.api.signature() != self.epoch:
            raise RouteChanged('选择或映射已变化')

    def node_probe(self, name, expected, pool):
        self.guard(expected, pool)
        port = self.api.probe_port(name, pool, self.probe_base)
        evidence = self.probe(f'http://127.0.0.1:{port}')
        self.guard(expected, pool)
        if not evidence.ok:
            self.api.probe_port(name, pool, self.probe_base)
        self.ledger.record(name, evidence, self.clock())
        return evidence

    def qualify(self, name, expected, pool, count, deadline):
        for index in range(count):
            if self.monotonic() >= deadline or self.cancel.is_set():
                raise RouteChanged('探测预算耗尽或停止请求')
            if not self.node_probe(name, expected, pool).ok:
                return False
            if self.monotonic() >= deadline or self.cancel.is_set():
                raise RouteChanged('探测预算耗尽或停止请求')
            if index + 1 < count and self.wait(.25):
                raise RouteChanged('停止请求')
        return True

    def rollback(self):
        pending = self.ledger.data['pending']
        if not pending:
            return
        current, pool = self.api.snapshot()
        if (current == pending['to'] and tuple(pending['pool']) == pool and pending['from'] in pool
                and pending['epoch'] == self.api.signature()):
            self.api.select(pending['from'])
            if self.api.snapshot() != (pending['from'], pool):
                raise RouteChanged('回滚未确认')
            self.log('route_rollback', node=pending['from'])
        elif current != pending['from']:
            self.log('route_external_selection_preserved', node=current)
        self.ledger.data['pending'] = None
        self.save()

    def commit(self, name, previous, pool, deadline):
        self.guard(previous, pool)
        # 通过准入后再做一次独立复核，热备也不能直接写选择器。
        if not self.qualify(name, previous, pool, 1, deadline):
            self.ledger.isolate(name, self.clock())
            return False
        if self.monotonic() >= deadline or self.cancel.is_set():
            raise RouteChanged('切换预算耗尽或停止请求')
        self.ledger.data['pending'] = {'from': previous, 'to': name, 'pool': list(pool), 'epoch': self.epoch}
        self.save()  # 写入之前保存事务，重启后可以安全恢复。
        try:
            self.guard(previous, pool)
            if self.monotonic() >= deadline:
                raise RouteChanged('切换预算耗尽')
            self.api.select(name)
            self.guard(name, pool)
            for _ in range(3):
                if self.monotonic() >= deadline or self.cancel.is_set():
                    raise RouteChanged('切换验证预算耗尽或停止请求')
                # 切换后的证据来自正式代理入口，不能用候选监听器冒充。
                evidence = self.probe(self.proxy)
                self.guard(name, pool)
                self.ledger.record(name, evidence, self.clock())
                if not evidence.ok:
                    break
                if self.monotonic() >= deadline or self.wait(.25):
                    raise RouteChanged('切换验证预算耗尽或停止请求')
            else:
                self.ledger.data['selected'] = name
                self.ledger.data['pending'] = None
                self.ledger.data['failures'] = []
                self.fail_streak = 0
                self.failure_started = 0
                self.recovery_level = 0
                self.save()
                self.log('route_committed', node=name)
                return True
        except RouteChanged:
            # 人工接管时不把选择器抢回来；rollback 会先检查当前值。
            self.rollback()
            raise
        except (OSError, ValueError):
            # API 超时可能发生在写入已生效之后；保留 pending 供下轮协调。
            self.rollback()
            raise
        self.ledger.isolate(name, self.clock())
        self.rollback()
        return False

    def recover(self, current, pool):
        deadline = self.monotonic() + 90
        candidates = self.ledger.ranked(pool, current, self.clock())
        if not candidates:
            candidates = self.ledger.ranked(pool, current, self.clock(), include_cooling=True)
            self.log('route_cooling_recheck')
        for name in candidates[:3]:
            if self.monotonic() >= deadline or self.cancel.is_set():
                break
            if not self.qualify(name, current, pool, 3, deadline):
                self.ledger.isolate(name, self.clock())
                continue
            if self.commit(name, current, pool, deadline):
                self.ledger.isolate(current, self.clock())
                self.save()
                return
        self.recovery_level = min(4, self.recovery_level + 1)
        self.next_recovery = self.clock() + (5, 15, 30, 60)[self.recovery_level - 1]
        self.save()
        self.log('route_recovery_deferred', delay=self.next_recovery - self.clock())

    def observe_traffic(self):
        try:
            connections = self.api.request('/connections').get('connections', [])
            if any(any(str(row.get('metadata', {}).get('host', '')).lower().rstrip('.') == domain or
                       str(row.get('metadata', {}).get('host', '')).lower().rstrip('.').endswith('.' + domain)
                       for domain in ('openai.com', 'chatgpt.com', 'oaistatic.com', 'oaiusercontent.com'))
                   for row in connections):
                self.last_traffic = self.clock()
        except (OSError, ValueError, AttributeError):
            pass

    def step(self):
        if not self.shadow:
            self.rollback()
        current, pool = self.api.snapshot()
        epoch = self.api.signature()
        if self.ledger.data.get('epoch') != epoch:
            self.ledger.data['nodes'] = {}
            self.ledger.data['failures'] = []
            self.ledger.data['epoch'] = epoch
            self.fail_streak = 0
        self.epoch = epoch
        self.ledger.prune(pool)
        now = self.clock()
        if self.ledger.data['selected'] != current:
            self.ledger.data['selected'] = current
            self.ledger.data['failures'] = []
            self.fail_streak = 0
            self.failure_started = 0
            self.next_recovery = 0
            self.recovery_level = 0
            self.log('route_selection_observed', node=current)
        result = self.probe(self.proxy)
        self.guard(current, pool)
        self.last_current_ok = result.ok
        self.ledger.record(current, result, now)
        self.log('route_current_probe', node=current, ok=result.ok, reason=result.reason, milliseconds=result.milliseconds)
        failures = [stamp for stamp in self.ledger.data['failures'] if 0 <= now - stamp <= 600]
        if result.ok:
            self.fail_streak = 0
            self.failure_started = 0
        else:
            failures.append(now)
            self.fail_streak += 1
            self.failure_started = self.failure_started or now
        self.ledger.data['failures'] = failures[-32:]
        self.observe_traffic()
        active = now - self.last_traffic < 90
        sustained = self.fail_streak >= (2 if active else 3) and now - self.failure_started >= (15 if active else 30)
        intermittent = not result.ok and (sum(now - stamp <= 300 for stamp in failures) >= 3 or len(failures) >= 4)
        if not self.shadow and (sustained or intermittent) and now >= self.next_recovery:
            self.recover(current, pool)
        elif now >= self.next_scan:
            candidates = self.ledger.ranked(pool, current, now)
            self.hot_nodes = [n for n in self.hot_nodes if n in candidates and self.ledger.node(n)['streak'] > 0]
            for name in candidates:
                if name not in self.hot_nodes and len(self.hot_nodes) < 2:
                    self.hot_nodes.append(name)
            cold = sorted((n for n in candidates if n not in self.hot_nodes), key=lambda n: self.ledger.node(n)['last'])
            selected = []
            if self.hot_nodes:
                selected.append(self.hot_nodes[self.hot_turn % len(self.hot_nodes)])
            if cold:
                if active and self.scan_turn % 3 == 2:
                    selected.append(cold[0])
                elif not active:
                    selected.append(cold[0])
            if self.hot_nodes:
                self.hot_turn += 1
            for name in selected:
                self.node_probe(name, current, pool)
            self.scan_turn += 1
            self.next_scan = now + 30
            self.log('route_standby_updated', ready=[n for n in self.hot_nodes if self.ledger.ready(n, now)])

        if not self.shadow:
            self.save()


def load_ledger(path):
    if not path.is_file():
        return {}
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('路由状态过大')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('schema') != 1 or not isinstance(value.get('nodes'), dict):
        raise ValueError('路由状态格式错误，请保留备份后检查')
    numeric = ('checks', 'successes', 'streak', 'level', 'until', 'last', 'good', 'latency', 'healthy_since', 'streak_since', 'quality')
    if len(value['nodes']) > 256 or not isinstance(value.get('selected', ''), str):
        raise ValueError('路由状态范围错误')
    for name, row in value['nodes'].items():
        if not isinstance(name, str) or not isinstance(row, dict):
            raise ValueError('节点状态格式错误')
        if any(type(row.get(key)) not in (int, float) or not math.isfinite(row[key]) or row[key] < 0 for key in numeric):
            raise ValueError('节点状态数值错误')
        if row['quality'] > 1 or row['level'] > 3:
            raise ValueError('节点状态数值越界')
    failures = value.get('failures', [])
    if not isinstance(failures, list) or len(failures) > 32 or any(type(t) not in (int, float) or not math.isfinite(t) or t < 0 for t in failures):
        raise ValueError('失败窗口格式错误')
    pending = value.get('pending')
    if pending is not None:
        if not isinstance(pending, dict) or not all(isinstance(pending.get(k), str) for k in ('from', 'to', 'epoch')):
            raise ValueError('未完成事务格式错误')
        if not isinstance(pending.get('pool'), list) or len(pending['pool']) > 256 or pending['from'] not in pending['pool'] or pending['to'] not in pending['pool']:
            raise ValueError('未完成事务候选错误')
    return value


def main():
    parser = argparse.ArgumentParser(description='openai-proxy-kit 路由引擎')
    parser.add_argument('--once', action='store_true', help='只读探测一轮，不修改选择或持久状态')
    parser.add_argument('--shadow', action='store_true', help='持续只读观察')
    parser.add_argument('--version', action='store_true')
    args = parser.parse_args()
    if args.version:
        print('openai-proxy-kit route engine ' + (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip())
        return 0
    shadow = args.once or args.shadow
    os.umask(0o077)
    state_path = Path(os.environ.get('STATE_PATH', Path.home() / '.local/share/openai-proxy-kit/controller/route-state.json'))
    log = JsonLog('' if shadow else os.environ.get('LOG_PATH', ''))
    try:
        api = Api(os.environ.get('MIHOMO_API', 'http://127.0.0.1:9090'), os.environ.get('MIHOMO_SECRET', ''),
                  os.environ.get('OPENAI_GROUP', 'OpenAI 自动选择'), os.environ.get('PROBE_MAP'), os.environ.get('MIHOMO_UNIT', ''))
        proxy = os.environ.get('MIHOMO_PROXY', 'http://127.0.0.1:7890')
        parsed = urllib.parse.urlsplit(proxy)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1') or parsed.username:
            raise ValueError('正式代理必须为回环 HTTP 地址')
        base = int(os.environ.get('PROBE_PORT_BASE', '17900'))
        if not 1 <= base <= 65280:
            raise ValueError('探测端口基址越界')
        lock = None
        if not shadow and not api.unit:
            raise ValueError('后台运行必须提供核心服务身份')
        if not shadow:
            directory = Path(os.environ.get('XDG_RUNTIME_DIR', Path.home() / '.cache')) / 'openai-proxy-kit'
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            identity = hashlib.sha256((api.base + '\n' + api.group).encode()).hexdigest()
            fd = os.open(directory / ('route-' + identity + '.lock'), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            lock = os.fdopen(fd, 'w')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        engine = Engine(api, CurlProbe(os.environ.get('CURL_PATH', '/usr/bin/curl')), load_ledger(state_path) if not shadow else {},
                        log, proxy, base, shadow)
        engine.save = lambda: private_write(state_path, json.dumps(engine.ledger.data, ensure_ascii=False))
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: engine.cancel.set())
        log('route_engine_started', shadow=shadow)
        while not engine.cancel.is_set():
            try:
                engine.step()
            except RouteChanged:
                log('route_evidence_discarded', level='warning')
                if args.once:
                    return 1
            except (OSError, ValueError, TypeError, KeyError):
                log('route_cycle_failed', level='error')
                if args.once:
                    return 1
            if args.once:
                return 0 if engine.last_current_ok else 1
            engine.cancel.wait(30 if engine.clock() - engine.last_traffic < 90 else 20)
        if not shadow and engine.ledger.data['pending']:
            engine.rollback()
        if lock:
            lock.close()
    except (OSError, ValueError, TypeError, KeyError):
        log('route_engine_unavailable', level='error')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

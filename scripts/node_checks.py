"""节点页的独立公开端点检测，不改变选择器或后台路由证据。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import subprocess
import time

from route_engine import Api, CurlProbe
from route_model import RouteChanged

FRESH_SECONDS = 300


class NodeChecker:
    def __init__(self, env):
        self.env = dict(env)
        self.directory = Path(env.get('MIHOMO_CONF_DIR', Path.home() / '.config/mihomo'))
        self.api = Api('http://127.0.0.1:' + env.get('API_PORT', '9090'),
                       env.get('MIHOMO_SECRET', ''), env.get('OPENAI_GROUP', 'OpenAI 自动选择'),
                       self.directory / 'probe-map.json', env.get('UNIT_MIHOMO', 'mihomo-kit.service'))
        self.base = int(env.get('PROBE_PORT_BASE', '17900'))

    def context(self):
        # 同名节点也可能更换服务器；配置、映射或核心进程变化均使旧结果失效。
        digest = hashlib.sha256()
        for value in (self.api.base.encode(), self.api.group.encode(), self.api.signature().encode(),
                      (self.directory / 'config.yaml').read_bytes(), self.api.mapping.read_bytes()):
            digest.update(value)
            digest.update(b'\0')
        return digest.hexdigest()

    def run(self):
        generation = self.context()
        _, pool = self.api.snapshot()

        def check(name):
            try:
                port = self.api.probe_port(name, pool, self.base)
                evidence = CurlProbe()('http://127.0.0.1:' + str(port))
                return name, {'ok': evidence.ok, 'milliseconds': round(evidence.milliseconds),
                              'checked_at': time.time()}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                # 入口不可信或不可读，不能据此判定节点失败，也不输出私有配置。
                return name, {'ok': None, 'milliseconds': 0, 'checked_at': time.time()}

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = dict(executor.map(check, pool))
        if self.context() != generation or self.api.snapshot()[1] != pool:
            raise RouteChanged('检测期间核心或配置已变更，请重新检测 ChatGPT')
        return {'generation': generation, 'results': results}


def check_context(env):
    try:
        return NodeChecker(env).context()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None


def node_rows(names, batch, generation, show_all=False, now=None):
    """同一套新鲜度及排序规则用于筛选和状态文字，避免过期结果仍显示绿色。"""
    now = time.time() if now is None else now
    valid = bool(generation) and batch.get('generation') == generation
    results = batch.get('results', {})
    rows = []
    for name in names:
        result = results.get(name)
        if result is None:
            state = '未检测'
        elif not valid or not 0 <= now - result['checked_at'] < FRESH_SECONDS:
            state = '已过期，请重测'
        else:
            state = ('公开端点可达' if result['ok'] is True else
                     '检测失败' if result['ok'] is False else '检测入口不可用')
        ok = state == '公开端点可达'
        if ok or show_all:
            rows.append({'name': name, 'ok': ok, 'state': state,
                         'milliseconds': result['milliseconds'] if ok else None,
                         'checked_at': result['checked_at'] if result else None})
    return sorted(rows, key=lambda row: (not row['ok'], row['milliseconds'] or 0, row['name']))

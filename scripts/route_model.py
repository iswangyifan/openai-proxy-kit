"""路由引擎的数据约定：节点顺序、证据窗口与有界冷却。"""
from dataclasses import dataclass
import hashlib
import json
from kit_state import private_write


def ordered_nodes(names):
    return sorted({n for n in names if isinstance(n, str) and n and n not in ('DIRECT', 'REJECT')},
                  key=lambda name: name.encode('utf-16-be'))


@dataclass(frozen=True)
class Evidence:
    ok: bool
    milliseconds: float = 0
    reason: str = ''


class RouteChanged(RuntimeError):
    """外部选择、节点映射或本地探测入口发生变化，不能处罚节点。"""


class Ledger:
    def __init__(self, data=None):
        data = data or {}
        self.data = {'schema': 1, 'nodes': data.get('nodes', {}), 'pending': data.get('pending'),
                     'epoch': data.get('epoch'), 'selected': data.get('selected', ''), 'failures': data.get('failures', [])}

    def node(self, name):
        return self.data['nodes'].setdefault(name, {'checks': 0, 'successes': 0, 'streak': 0,
                                                    'level': 0, 'until': 0, 'last': 0, 'good': 0,
                                                    'latency': 0, 'healthy_since': 0, 'streak_since': 0, 'quality': .5})

    def record(self, name, result, now):
        item = self.node(name)
        previous = item['last']
        item['last'] = now
        item['checks'] += 1
        item['quality'] = .75 * item.get('quality', .5) + .25 * int(result.ok)
        if result.ok:
            item['successes'] += 1
            continuing = item['streak'] > 0 and now - previous <= 120
            item['streak'] = item['streak'] + 1 if continuing else 1
            if not continuing:
                item['streak_since'] = now
            item['good'] = now
            item['healthy_since'] = now if not previous or now - previous > 120 else item['healthy_since'] or now
            item['latency'] = result.milliseconds if not item['latency'] else .7 * item['latency'] + .3 * result.milliseconds
            if now - previous <= 120 and now - item['healthy_since'] >= 1800:
                item['level'] = max(0, item['level'] - 1)
                item['healthy_since'] = now
        else:
            item['streak'] = 0
            item['streak_since'] = 0
            item['healthy_since'] = 0

    def isolate(self, name, now):
        item = self.node(name)
        if item['until'] <= now:
            item['level'] = min(3, item['level'] + 1)
            item['until'] = now + (300, 900, 1800)[item['level'] - 1]
        item['streak'] = 0

    def ready(self, name, now):
        item = self.node(name)
        return item['streak'] >= 3 and now - item.get('streak_since', now) >= 45 and 0 <= now - item['good'] <= 90

    def ranked(self, names, current, now, include_cooling=False):
        candidates = [name for name in ordered_nodes(names) if name != current]
        candidates = [name for name in candidates if include_cooling or self.node(name)['until'] <= now]
        # 新鲜连续成功优先；未知候选保留探索机会，不依赖共享 alive 缓存。
        return sorted(candidates, key=lambda name: (
            not self.ready(name, now),
            -(.5 + (self.node(name).get('quality', .5) - .5) * .5 ** (max(0, now - self.node(name)['last']) / 600)),
            self.node(name)['last']))

    def prune(self, names):
        keep = set(names)
        self.data['nodes'] = {name: value for name, value in self.data['nodes'].items() if name in keep}


def write_probe_map(config, path):
    bindings = {row['proxy']: row['port'] for row in config.get('listeners', [])
                if row.get('type') == 'mixed' and row.get('listen') == '127.0.0.1'
                and isinstance(row.get('proxy'), str) and type(row.get('port')) is int}
    signature = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    private_write(path, json.dumps({'generation': signature, 'bindings': bindings}, ensure_ascii=False))

"""代理网站的私有设置、规则合并、配置事务及按需公开端点检测。"""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import yaml
from kit_state import load as load_state, private_write
from route_model import write_probe_map

PRESETS = {
    'github': ('github.com', 'githubusercontent.com', 'githubassets.com', 'github.io'),
    'youtube': ('youtube.com', 'youtu.be', 'youtube-nocookie.com', 'googlevideo.com',
                'ytimg.com', 'ggpht.com', 'youtubei.googleapis.com',
                'youtube.googleapis.com', 'accounts.google.com',
                'ytimg.l.google.com', 'youtube.l.google.com'),
}
OPENAI_DOMAINS = ('openai.com', 'chatgpt.com', 'oaistatic.com', 'oaiusercontent.com', 'cdn.auth0.com')
PRIVATE_RULES = [f'IP-CIDR,{net},DIRECT,no-resolve' for net in
                 ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
PRIVATE_RULES += [f'IP-CIDR6,{net},DIRECT,no-resolve' for net in ('::1/128', 'fc00::/7', 'fe80::/10')]


def normalize(value):
    if not isinstance(value, dict) or set(value) - {'schema', 'enabled', 'domains'}:
        raise ValueError('网站设置格式无效')
    if type(value.get('schema', 1)) is not int or value.get('schema', 1) != 1:
        raise ValueError('不支持的网站设置版本')
    enabled, domains = value.get('enabled', []), value.get('domains', [])
    if not isinstance(enabled, list) or any(not isinstance(x, str) or x not in PRESETS for x in enabled):
        raise ValueError('网站预设无效')
    if not isinstance(domains, list) or len(domains) > 128:
        raise ValueError('自定义域名最多 128 行')
    cleaned = []
    for index, domain in enumerate(domains, 1):
        try:
            if not isinstance(domain, str):
                raise ValueError()
            domain = domain.strip().rstrip('.').encode('idna').decode('ascii').lower()
            if (len(domain) > 253 or len(domain.split('.')) < 2 or
                    not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in domain.split('.')) or
                    domain.endswith(('.local', '.localhost', '.lan', '.internal')) or
                    domain.rsplit('.', 1)[-1].isdigit()):
                raise ValueError()
            try:
                ipaddress.ip_address(domain)
            except ValueError:
                pass
            else:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise ValueError(f'第 {index} 行不是有效的公开域名；请移除协议、端口、路径或通配符') from None
        if domain not in cleaned:
            cleaned.append(domain)
    return {'schema': 1, 'enabled': sorted(set(enabled)), 'domains': cleaned}


def location(env):
    return Path(env.get('INSTALL_HOME', Path.home() / '.local/share/openai-proxy-kit')) / 'website-routing.json'


def load_profile(path, config=None, group='OpenAI 自动选择'):
    path = Path(path)
    if path.exists():
        if path.stat().st_size > 65536:
            raise ValueError('网站设置文件过大')
        return normalize(json.loads(path.read_text()))
    # 兼容曾经手动添加预设主域名的安装。
    rules = (config or {}).get('rules', [])
    enabled = [key for key, domains in PRESETS.items() if f'DOMAIN-SUFFIX,{domains[0]},{group}' in rules]
    return normalize({'enabled': enabled})


def read_config(path):
    path = Path(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('核心配置文件过大')
    try:
        value = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        raise ValueError('核心配置 YAML 无效；为保护节点凭据不显示原文') from None
    if not isinstance(value, dict) or not isinstance(value.get('rules'), list):
        raise ValueError('核心配置格式无效')
    return value


def current_profile(env):
    prefs = location(env)
    if prefs.is_file():
        return load_profile(prefs)
    config = Path(env['MIHOMO_CONF_DIR']) / 'config.yaml' if env.get('MIHOMO_CONF_DIR') else None
    return load_profile(prefs, read_config(config) if config and config.is_file() else None,
                        env.get('OPENAI_GROUP', 'OpenAI 自动选择'))


def merge_rules(config, profile, group, previous=None):
    profile = normalize(profile)
    if not any(g.get('name') == group for g in config.get('proxy-groups', [])):
        raise ValueError('找不到当前代理组，请先完成订阅配置')
    managed = {d for domains in PRESETS.values() for d in domains}
    remove = {f'DOMAIN-SUFFIX,{d},{group}' for d in managed}
    remove.update(f'DOMAIN-SUFFIX,{d},DIRECT' for d in managed)
    remove.update(f'DOMAIN-SUFFIX,{d},{group}' for d in (previous or {}).get('domains', []))
    keep = [r for r in config['rules'] if isinstance(r, str) and r not in remove and
            r not in PRIVATE_RULES and not r.startswith('MATCH,')]
    # 自定义域名独立生效，优先于预设关闭时添加的 DIRECT。
    additions = [f'DOMAIN-SUFFIX,{domain},{group}' for domain in profile['domains']]
    additions += [f'DOMAIN-SUFFIX,{domain},{group if key in profile["enabled"] else "DIRECT"}'
                  for key, domains in PRESETS.items() for domain in domains]
    # kit 管理的域名优先于保留的旧规则；其余流量仍默认直连。
    mandatory = [f'DOMAIN-SUFFIX,{domain},{group}' for domain in OPENAI_DOMAINS]
    config['rules'] = list(dict.fromkeys(PRIVATE_RULES + mandatory + additions + keep + ['MATCH,DIRECT']))
    return config


def active(env):
    return subprocess.run(['systemctl', '--user', 'is-active', '--quiet', env['UNIT_MIHOMO']],
                          capture_output=True, timeout=5).returncode == 0


def assert_owner(env):
    result = subprocess.run(['systemctl', '--user', 'show', env['UNIT_MIHOMO'], '-p', 'MainPID', '--value'],
                            capture_output=True, text=True, timeout=5)
    pid = result.stdout.strip()
    if result.returncode or not pid.isdecimal() or int(pid) <= 0:
        raise RuntimeError('无法确认本工具核心身份，未重载配置')
    for port in (env['API_PORT'], env['MIXED_PORT']):
        listeners = subprocess.run(['ss', '-tlnpH', 'sport = :' + port], capture_output=True,
                                   text=True, timeout=5)
        if listeners.returncode or f'pid={pid},' not in listeners.stdout:
            raise RuntimeError('管理或代理端口属于其他进程，未重载配置')


def api(env, path, value=None, method=None):
    request = urllib.request.Request('http://127.0.0.1:' + env['API_PORT'] + path,
        headers={'Authorization': 'Bearer ' + env.get('MIHOMO_SECRET', ''), 'Content-Type': 'application/json'},
        data=json.dumps(value).encode() if value is not None else None,
        method=method or ('PUT' if value is not None else 'GET'))
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with client.open(request, timeout=8) as response:
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError('核心响应过大')
        return json.loads(body) if body else {}


def validate_config(env, config_path, candidate):
    result = subprocess.run([env['MIHOMO_BIN'], '-t', '-d', str(Path(config_path).parent), '-f', str(candidate)],
                            capture_output=True, timeout=45)
    if result.returncode:
        raise ValueError('核心配置校验失败，未更改已保存设置')


def reload_core(env, config_path):
    assert_owner(env)
    api(env, '/configs?force=true', {'path': str(config_path.resolve())})


def restore(env, journal):
    # 路径由当前安装状态派生，绝不信任恢复文件提供的任意文件名。
    config = Path(env['MIHOMO_CONF_DIR']) / 'config.yaml'
    mapping = config.parent / 'probe-map.json'
    prefs = location(env)
    if journal.stat().st_size > 24 * 1024 * 1024:
        raise ValueError('网站恢复记录过大')
    record = json.loads(journal.read_text())
    if (not isinstance(record, dict) or set(record) != {'config', 'map', 'prefs', 'live'} or
            not isinstance(record['live'], bool) or
            any(v is not None and not isinstance(v, str) for k, v in record.items() if k != 'live')):
        raise ValueError('网站恢复记录格式无效')
    for key, path in (('config', config), ('map', mapping), ('prefs', prefs)):
        value = record[key]
        if value is None:
            path.unlink(missing_ok=True)
        elif isinstance(value, str):
            private_write(path, value)
        else:
            raise ValueError('网站恢复内容无效')
    if record['live'] and active(env):
        reload_core(env, config)
    journal.unlink()


def apply_profile(env, profile):
    profile = normalize(profile)
    prefs = location(env)
    prefs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = prefs.with_name('website-routing.pending.json')
    config = Path(env['MIHOMO_CONF_DIR']) / 'config.yaml' if env.get('MIHOMO_CONF_DIR') else None
    if journal.exists():
        restore(env, journal)
    if config is None or not config.is_file():
        private_write(prefs, json.dumps(profile, ensure_ascii=False))
        return {'configured': False, 'live': False}
    old = read_config(config)
    group = env.get('OPENAI_GROUP', 'OpenAI 自动选择')
    previous = load_profile(prefs, old, group)
    changed = merge_rules(old, profile, group, previous)
    original = config.read_text()
    header = []
    for line in original.splitlines(keepends=True):
        if not line.strip() or line.startswith('#'):
            header.append(line)
        else:
            break
    rendered = ''.join(header) + yaml.safe_dump(changed, allow_unicode=True, sort_keys=False)
    fd, filename = tempfile.mkstemp(prefix='.websites-', suffix='.yaml', dir=config.parent)
    temporary = Path(filename)
    os.close(fd)
    try:
        private_write(temporary, rendered)
        validate_config(env, config, temporary)
    finally:
        temporary.unlink(missing_ok=True)
    live = active(env)
    if live:
        assert_owner(env)
        api(env, '/version')
    mapping = config.parent / 'probe-map.json'
    record = {'config': original, 'map': mapping.read_text() if mapping.exists() else None,
              'prefs': prefs.read_text() if prefs.exists() else None, 'live': live}
    private_write(journal, json.dumps(record, ensure_ascii=False))
    try:
        private_write(config, rendered)
        write_probe_map(changed, mapping)
        private_write(prefs, json.dumps(profile, ensure_ascii=False))
        if live:
            reload_core(env, config)
        journal.unlink()
    except BaseException:
        try:
            restore(env, journal)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            raise RuntimeError('保存失败且恢复尚未完成；请先检查核心服务，再重新保存以重试恢复') from None
        raise
    return {'configured': True, 'live': live}


def probe_services(env, profile):
    from route_engine import CurlProbe
    assert_owner(env)
    group = '/proxies/' + urllib.parse.quote(env.get('OPENAI_GROUP', 'OpenAI 自动选择'), safe='')
    selected = api(env, group).get('now')
    config = Path(env['MIHOMO_CONF_DIR']) / 'config.yaml'
    generation = config.read_bytes()
    proxy = 'http://127.0.0.1:' + env['MIXED_PORT']
    result = {}
    evidence = CurlProbe()(proxy)
    result['openai'] = {'ok': evidence.ok, 'detail': '公开端点可达' if evidence.ok else '公开端点检测失败'}
    for key, url in (('github', 'https://api.github.com/meta'), ('youtube', 'https://www.youtube.com/robots.txt')):
        if key not in profile['enabled']:
            result[key] = {'ok': None, 'detail': '预设未启用'}
            continue
        with tempfile.TemporaryDirectory(prefix='opk-site-check-') as tmp:
            body = Path(tmp) / 'body'
            start = time.monotonic()
            try:
                request = subprocess.run(['/usr/bin/curl', '-q', '--silent', '--show-error', '--proxy', proxy,
                    '--noproxy', '', '--proto', '=https', '--connect-timeout', '4', '--max-time', '8',
                    '--max-filesize', '524288', '--output', str(body), '--write-out', '%{http_code}', url],
                    capture_output=True, text=True, timeout=10)
                code = request.stdout.strip()
                content = body.read_bytes() if body.exists() else b''
                ok = not request.returncode and code == '200' and bool(content)
                if ok:
                    ok = (isinstance(json.loads(content), dict) and 'ssh_key_fingerprints' in json.loads(content)) if key == 'github' else b'user-agent:' in content.lower()
                result[key] = {'ok': ok, 'detail': (f'公开端点可达 · {time.monotonic()-start:.1f}s' if ok else
                               '连接失败' if request.returncode else 'HTTP ' + code + ' 或响应不符合预期')}
            except (OSError, ValueError, subprocess.SubprocessError):
                result[key] = {'ok': False, 'detail': '连接失败或响应无效'}
    assert_owner(env)
    if api(env, group).get('now') != selected or config.read_bytes() != generation:
        raise RuntimeError('检测期间节点或规则已变更，请重新检测')
    return result


def main():
    parser = argparse.ArgumentParser(description='代理网站设置')
    parser.add_argument('--state', default=os.environ.get('KIT_ENV', str(Path.home() / '.local/share/openai-proxy-kit/kit.env')))
    parser.add_argument('--apply', action='store_true', help='从标准输入读取并保存网站设置')
    args = parser.parse_args()
    os.umask(0o077)
    state = Path(args.state)
    try:
        if not args.apply:
            env = load_state(state) if state.is_file() else {'INSTALL_HOME': str(state.parent)}
            print(json.dumps(current_profile(env), ensure_ascii=False))
            return 0
        state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(str(state) + '.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('另一个配置或启停操作正在进行，请稍后重试') from None
            env = load_state(state) if state.is_file() else {'INSTALL_HOME': str(state.parent)}
            payload = sys.stdin.read(65537)
            if len(payload) > 65536:
                raise ValueError('网站设置过大')
            def interrupted(*_):
                raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM, interrupted)
            print(json.dumps(apply_profile(env, json.loads(payload))))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # 不输出 YAML、响应体或订阅凭据。
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) and not isinstance(exc, json.JSONDecodeError) else '网站设置操作失败，请检查服务和私有配置'
        print(message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('网站设置操作已取消', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())

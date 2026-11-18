"""破坏性发行验收：仅允许在明确标记的临时虚拟机执行。

不依赖开发源码；导入和命令均来自已安装的软件包。
订阅由调用者以私有文件提供，报告不记录节点名、订阅或密钥。
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

APP = Path('/usr/lib/openai-proxy-kit')
HOME = Path.home()
STATE = HOME / '.local/share/openai-proxy-kit/kit.env'
REPORT = HOME / 'accept/results.json'
os.environ['XDG_RUNTIME_DIR'] = f'/run/user/{os.getuid()}'
os.environ['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path=/run/user/{os.getuid()}/bus'


def run(*args, ok=True, env=None, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env=dict(os.environ, **(env or {})))
    if ok and result.returncode:
        # 原始诊断仅存在虚拟机私有文件，不进入公开报告。
        (REPORT.parent / 'last-error.log').write_text(result.stdout + result.stderr)
        raise AssertionError(f'{args[0]} failed ({result.returncode}); see private last-error.log')
    return result


def record(name, details=None):
    rows = json.loads(REPORT.read_text()) if REPORT.exists() else []
    rows.append({'case': name, 'result': 'PASS', 'details': details or {}})
    REPORT.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    print('PASS:', name, flush=True)


def settings(action, protocol, key, value=None):
    args = ['gsettings', action, 'org.gnome.system.proxy' + ('.' + protocol if protocol else ''), key]
    if value is not None:
        args.append(str(value))
    return run(*args).stdout.strip()


def snapshot():
    return {'mode': settings('get', '', 'mode'), **{
        p: {k: settings('get', p, k) for k in ('host', 'port')}
        for p in ('http', 'https', 'ftp', 'socks')}}


def state():
    sys.path.insert(0, str(APP / 'scripts'))
    from kit_state import load
    return load(STATE)


def wait_for(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.5)
    raise AssertionError('condition timeout')


def active(unit):
    return run('systemctl', '--user', 'is-active', '--quiet', unit, ok=False).returncode == 0


def api(path, value=None, authorized=True):
    env = state()
    headers = {'Authorization': 'Bearer ' + env['MIHOMO_SECRET']} if authorized else {}
    if value is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request('http://127.0.0.1:' + env['API_PORT'] + path,
        headers=headers, data=json.dumps(value).encode() if value is not None else None,
        method='PUT' if value is not None else 'GET')
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as res:
        payload = res.read()
        return json.loads(payload) if payload else {}


def configure():
    assert run('which', 'node', ok=False).returncode != 0
    assert run('which', 'git', ok=False).returncode != 0
    assert not STATE.exists()
    baseline = snapshot()
    custom = HOME / '.local/share/applications/cursor.desktop'
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.write_text('[Desktop Entry]\nType=Application\nName=Acceptance custom\nExec=/bin/true --user-data-dir=acceptance\n')
    (HOME / 'accept/cursor-original').write_bytes(custom.read_bytes())
    run('openai-proxy-kit', 'configure', env={
        'SUB_URL': str(HOME / 'accept/subscription.yaml'), 'ASSUME_YES': '1'})
    env = state()
    assert active(env['UNIT_MIHOMO']) and active(env['UNIT_CONTROLLER'])
    assert settings('get', '', 'mode') == "'manual'"
    assert settings('get', 'http', 'port') == env['MIXED_PORT']
    assert STATE.stat().st_mode & 0o077 == 0
    assert (Path(env['MIHOMO_CONF_DIR']) / 'probe-map.json').stat().st_mode & 0o077 == 0
    from urllib.error import HTTPError
    try:
        api('/version', authorized=False)
        raise AssertionError('management API accepted missing token')
    except HTTPError as exc:
        assert exc.code == 401
    assert 'version' in api('/version')
    record('fresh_configure_without_node_git_or_controller_checkout')
    record('private_state_and_authenticated_management_api')
    assert custom.read_bytes() == (HOME / 'accept/cursor-original').read_bytes()
    assert not (custom.parent / 'code.desktop').exists()
    run('bash', str(APP / 'install.sh'), 'desktop', env={'DESKTOP_INJECT': '1', 'ASSUME_YES': '1'})
    assert '--user-data-dir=acceptance' in custom.read_text()
    assert 'http_proxy' in custom.read_text()
    record('explicit_launcher_adaptation_preserves_user_arguments')
    run('openai-proxy-kit', 'start')
    assert active(env['UNIT_MIHOMO']) and active(env['UNIT_CONTROLLER'])
    assert settings('get', '', 'mode') == "'manual'"
    record('repeated_start_is_idempotent_with_adapted_launchers')
    run('openai-proxy-kit', 'stop')
    assert snapshot() == baseline
    assert not active(env['UNIT_MIHOMO'])
    assert custom.read_bytes() == (HOME / 'accept/cursor-original').read_bytes()
    record('normal_stop_restores_all_original_proxy_fields')


def conflicts():
    env = state()
    baseline = snapshot()
    server = socket.socket()
    server.bind(('127.0.0.1', int(env['MIXED_PORT'])))
    server.listen()
    try:
        assert run('openai-proxy-kit', 'start', ok=False).returncode != 0
        assert snapshot() == baseline
    finally:
        server.close()
    record('foreign_process_port_conflict_fails_without_proxy_change')
    with open(str(STATE) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run('openai-proxy-kit', 'start', ok=False).returncode != 0
        assert snapshot() == baseline
    record('concurrent_operation_lock_fails_without_side_effect')
    settings('set', 'http', 'host', '127.0.0.2')
    settings('set', 'http', 'port', '18888')
    settings('set', '', 'mode', 'manual')
    foreign = snapshot()
    run('openai-proxy-kit', 'start')
    run('openai-proxy-kit', 'stop')
    assert snapshot() == foreign
    record('existing_manual_proxy_restored_exactly')
    settings('set', '', 'mode', 'auto')
    settings('set', '', 'autoconfig-url', 'http://127.0.0.2/company.pac')
    pac = snapshot()
    run('openai-proxy-kit', 'start')
    run('openai-proxy-kit', 'stop')
    assert snapshot() == pac
    assert settings('get', '', 'autoconfig-url') == "'http://127.0.0.2/company.pac'"
    record('existing_pac_proxy_preserved')
    settings('set', '', 'mode', 'none')
    settings('set', 'http', 'host', '')
    settings('set', 'http', 'port', '0')
    run('openai-proxy-kit', 'start')
    settings('set', 'https', 'host', '127.0.0.3')
    settings('set', 'https', 'port', '18889')
    run('openai-proxy-kit', 'stop')
    assert settings('get', 'https', 'host') == "'127.0.0.3'"
    assert settings('get', 'https', 'port') == '18889'
    assert settings('get', 'http', 'port') == '0'
    assert settings('get', '', 'mode') == "'manual'"
    record('foreign_https_change_preserved_while_owned_http_restored')
    settings('set', 'https', 'host', '')
    settings('set', 'https', 'port', '0')
    settings('set', '', 'mode', 'none')


def crash():
    env = state()
    run('openai-proxy-kit', 'start')
    old = run('systemctl', '--user', 'show', env['UNIT_MIHOMO'], '-p', 'MainPID', '--value').stdout.strip()
    run('systemctl', '--user', 'kill', '--kill-whom=main', '--signal=KILL', env['UNIT_MIHOMO'])
    wait_for(lambda: active(env['UNIT_MIHOMO']) and run('systemctl', '--user', 'show',
        env['UNIT_MIHOMO'], '-p', 'MainPID', '--value').stdout.strip() not in ('0', old))
    wait_for(lambda: settings('get', '', 'mode') == "'manual'")
    assert api('/version')
    run('openai-proxy-kit', 'stop')
    assert settings('get', '', 'mode') == "'none'"
    record('real_core_sigkill_restart_and_proxy_intent_recovery')


def update_rollback():
    import yaml
    env = state()
    run('openai-proxy-kit', 'start')
    conf = Path(env['MIHOMO_CONF_DIR']) / 'config.yaml'
    mapping = conf.parent / 'probe-map.json'
    original, original_map = conf.read_bytes(), mapping.read_bytes()
    sub = HOME / 'accept/subscription.yaml'
    original_sub = sub.read_bytes()
    value = yaml.safe_load(original_sub)
    value['proxies'] = value['proxies'][:2]
    value['proxies'][0]['name'] = 'acceptance-replacement'
    sub.write_text(yaml.safe_dump(value))
    dropin = HOME / '.config/systemd/user' / (env['UNIT_MIHOMO'] + '.d')
    dropin.mkdir()
    broken = dropin / 'acceptance.conf'
    broken.write_text('[Service]\nExecStart=\nExecStart=/nonexistent/opk-acceptance\n')
    try:
        run('systemctl', '--user', 'daemon-reload')
        assert run('openai-proxy-kit', 'update', ok=False).returncode != 0
        assert conf.read_bytes() == original
        assert mapping.read_bytes() == original_map
        record('failed_subscription_update_restores_config_and_probe_map')
    finally:
        broken.unlink()
        dropin.rmdir()
        sub.write_bytes(original_sub)
        run('systemctl', '--user', 'daemon-reload')
        run('systemctl', '--user', 'reset-failed', env['UNIT_MIHOMO'])
        run('systemctl', '--user', 'restart', env['UNIT_MIHOMO'])
        run('openai-proxy-kit', 'stop')


def connectivity():
    import threading
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    sys.path.insert(0, str(APP / 'scripts'))
    from route_engine import CurlProbe
    env = state()
    run('openai-proxy-kit', 'start')
    run('systemctl', '--user', 'stop', env['UNIT_CONTROLLER'])
    group = '/proxies/' + urllib.parse.quote(env['OPENAI_GROUP'], safe='')
    names = api(group)['all']
    proxy = 'http://127.0.0.1:' + env['MIXED_PORT']
    probe = CurlProbe()
    passed = False
    checked = 0
    for name in names:
        api(group, {'name': name})
        checked += 1
        if probe(proxy).ok:
            passed = True
            break
    assert passed, 'no node passed public HTTPS trace/robots semantic checks'
    record('real_subscription_public_https_semantic_probe', {'nodes_checked': checked,
        'scope': 'public endpoints; excludes account login and model invocation'})

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'opk-intranet-fixture')

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = run('curl', '--silent', '--show-error', '--noproxy', '', '--proxy', proxy,
                     '--max-time', '10', f'http://127.0.0.1:{server.server_port}/')
        assert result.stdout == 'opk-intranet-fixture'
        record('proxy_enabled_private_destination_direct_fixture')
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    run('openai-proxy-kit', 'stop')


def failover():
    import copy
    import yaml
    import urllib.parse
    assert not STATE.exists()
    nodes = yaml.safe_load((HOME / 'accept/subscription.yaml').read_text())['proxies']
    good = copy.deepcopy(nodes[0])
    good['name'] = 'Acceptance-B-working'
    bad = {'name': 'Acceptance-A-dead', 'type': 'http', 'server': '127.0.0.1', 'port': 1}
    sub = HOME / 'accept/failover.yaml'
    sub.write_text(yaml.safe_dump({'proxies': [bad, good]}))
    run('openai-proxy-kit', 'configure', env={'SUB_URL': str(sub), 'ASSUME_YES': '1'})
    env = state()
    group = '/proxies/' + urllib.parse.quote(env['OPENAI_GROUP'], safe='')
    started = time.monotonic()

    def committed():
        ledger = Path(env['INSTALL_HOME']) / 'controller/route-state.json'
        if not ledger.exists():
            return False
        value = json.loads(ledger.read_text())
        return (value.get('selected') == good['name'] and value.get('pending') is None
                and api(group)['now'] == good['name'])

    wait_for(committed, timeout=150)
    assert active(env['UNIT_CONTROLLER'])
    journal = run('journalctl', '--user', '-u', env['UNIT_CONTROLLER'], '-n', '80', '--no-pager').stdout
    assert 'route_committed' in journal
    record('real_mihomo_automatic_failover_from_dead_node',
           {'seconds_after_configure': round(time.monotonic() - started, 1)})
    run('openai-proxy-kit', 'stop')


def network_boundary():
    import yaml
    resolver = Path('/etc/resolv.conf').read_bytes()
    run('openai-proxy-kit', 'start')
    env = state()
    folder = Path(env['MIHOMO_CONF_DIR'])
    config = yaml.safe_load((folder / 'config.yaml').read_text())
    assert config.get('tun', {}).get('enable', False) is False
    assert config['dns']['listen'] == '127.0.0.1:' + env['DNS_PORT']
    assert Path('/etc/resolv.conf').read_bytes() == resolver
    assert not list(folder.glob('*.dat')) and not list(folder.glob('*.mmdb'))
    for port in (env['MIXED_PORT'], env['API_PORT'], env['PROBE_PORT_BASE']):
        rows = run('ss', '-tlnH', 'sport = :' + port).stdout.splitlines()
        assert rows and all(row.split()[3] in ('127.0.0.1:' + port, '[::1]:' + port) for row in rows)
    record('loopback_listeners_local_dns_no_system_resolver_change_or_geo_database')
    run('openai-proxy-kit', 'stop')


def reboot_before():
    run('openai-proxy-kit', 'start')
    (HOME / 'accept/boot-before').write_text(Path('/proc/sys/kernel/random/boot_id').read_text())
    record('enabled_services_and_proxy_before_reboot')


def reboot_after():
    assert (HOME / 'accept/boot-before').read_text() != Path('/proc/sys/kernel/random/boot_id').read_text()
    env = state()
    wait_for(lambda: active(env['UNIT_MIHOMO']))
    wait_for(lambda: active(env['UNIT_CONTROLLER']))
    wait_for(lambda: settings('get', '', 'mode') == "'manual'")
    assert api('/version')
    run('openai-proxy-kit', 'stop')
    assert settings('get', '', 'mode') == "'none'"
    record('real_vm_reboot_autostart_and_clean_stop')


def removed():
    assert run('which', 'openai-proxy-kit', ok=False).returncode != 0
    assert not STATE.exists()
    assert settings('get', '', 'mode') == "'none'"
    assert settings('get', 'http', 'port') == '0'
    assert settings('get', 'https', 'port') == '0'
    assert not list((HOME / '.config/systemd/user').glob('*-kit.service'))
    assert not (HOME / '.config/systemd/user/opk-route.service').exists()
    assert not (HOME / '.local/share/applications/code.desktop').exists()
    assert (HOME / '.local/share/applications/cursor.desktop').read_bytes() == (HOME / 'accept/cursor-original').read_bytes()
    assert (HOME / '.local/share/openai-proxy-kit/backup/desktop/manifest.json').exists()
    record('apt_remove_restores_proxy_launchers_and_removes_managed_units')


def upgrade_before():
    import hashlib
    import yaml
    env = state()
    value = {'unit': env['UNIT_CONTROLLER'], 'proxy': snapshot(),
             'secret_hash': hashlib.sha256(env['MIHOMO_SECRET'].encode()).hexdigest(),
             'nodes': yaml.safe_load((Path(env['MIHOMO_CONF_DIR']) / 'config.yaml').read_text())['proxies']}
    # 含节点配置，只保存于虚拟机内的私有测试目录。
    (HOME / 'accept/upgrade-before.json').write_text(json.dumps(value))
    assert active(env['UNIT_MIHOMO'])
    record('old_version_configured_before_apt_upgrade',
           {'route_service_running': active(env['UNIT_CONTROLLER'])})


def upgrade_after():
    import hashlib
    import yaml
    before = json.loads((HOME / 'accept/upgrade-before.json').read_text())
    assert snapshot() == before['proxy'], 'apt upgrade must not remove proxy state'
    run('openai-proxy-kit', 'upgrade')
    env = state()
    assert active(env['UNIT_MIHOMO']) and active(env['UNIT_CONTROLLER'])
    assert env['UNIT_CONTROLLER'] == 'opk-route.service'
    assert hashlib.sha256(env['MIHOMO_SECRET'].encode()).hexdigest() == before['secret_hash']
    assert yaml.safe_load((Path(env['MIHOMO_CONF_DIR']) / 'config.yaml').read_text())['proxies'] == before['nodes']
    assert snapshot() == before['proxy']
    if before['unit'] != env['UNIT_CONTROLLER']:
        assert not (HOME / '.config/systemd/user' / before['unit']).exists()
    record('real_apt_upgrade_preserves_nodes_auth_proxy_and_migrates_service')
    run('openai-proxy-kit', 'stop')
    run('openai-proxy-kit', 'upgrade')
    assert not active(env['UNIT_MIHOMO']) and not active(env['UNIT_CONTROLLER'])
    assert settings('get', '', 'mode') == "'none'"
    record('cold_upgrade_keeps_services_and_proxy_stopped')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['lifecycle', 'connectivity', 'before-reboot', 'after-reboot',
                                         'removed', 'upgrade-before', 'upgrade-after', 'failover', 'network-boundary'])
    args = parser.parse_args()
    if not Path('/etc/opk-acceptance-vm').exists() or not socket.gethostname().startswith('opk-acceptance'):
        raise SystemExit('Refusing destructive acceptance outside the marked disposable VM')
    REPORT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    if args.phase == 'lifecycle':
        configure()
        conflicts()
        crash()
        update_rollback()
    else:
        {'connectivity': connectivity, 'before-reboot': reboot_before,
         'after-reboot': reboot_after, 'removed': removed,
         'upgrade-before': upgrade_before, 'upgrade-after': upgrade_after,
         'failover': failover, 'network-boundary': network_boundary}[args.phase]()

"""GNOME 代理的保存与逐协议恢复；不强制抢回其他应用的代理。"""
import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
from kit_state import load, private_write

PROTOCOLS = ('http', 'https', 'ftp', 'socks')


def settings(action, schema, key, value=None):
    command = ['gsettings', action, 'org.gnome.system.proxy' + ('.' + schema if schema else ''), key]
    if value is not None:
        command.append(value)
    return subprocess.run(command, capture_output=True, text=True, check=True, timeout=10).stdout.strip()


def current():
    return {'mode': settings('get', '', 'mode'),
            **{p: {'host': settings('get', p, 'host'), 'port': settings('get', p, 'port')}
               for p in PROTOCOLS}}


def owned(target, port):
    return target['host'].strip("'") == '127.0.0.1' and target['port'] == str(port)


def snapshot_path(env):
    return Path(env['INSTALL_HOME']) / 'system-proxy-before.json'


def start(env):
    path = snapshot_path(env)
    record = json.loads(path.read_text()) if path.exists() else {'before': current()}
    if 'before' not in record:
        record = {'before': record}
    record['phase'] = 'starting'
    private_write(path, json.dumps(record))
    try:
        for protocol in ('http', 'https'):
            settings('set', protocol, 'host', '127.0.0.1')
            settings('set', protocol, 'port', env['MIXED_PORT'])
        settings('set', '', 'mode', 'manual')
        record['phase'] = 'active'
        private_write(path, json.dumps(record))
        private_write(Path(env['INSTALL_HOME']) / 'proxy-enabled', 'enabled\n')
    except (OSError, ValueError, subprocess.SubprocessError):
        stop(env)
        raise


def stop(env, clear_intent=True):
    if clear_intent:
        (Path(env['INSTALL_HOME']) / 'proxy-enabled').unlink(missing_ok=True)
    path = snapshot_path(env)
    now = current()
    record = json.loads(path.read_text()) if path.exists() else {}
    before = record.get('before', record) or None
    if record.get('phase') == 'starting':
        # 写入意图先持久化：即使写 host 后在 port 处失败，也能逐字段恢复。
        for protocol in ('http', 'https'):
            for key, expected in (('host', "'127.0.0.1'"), ('port', env['MIXED_PORT'])):
                if now[protocol][key] == expected and before[protocol][key] != expected:
                    settings('set', protocol, key, before[protocol][key])
        if now['mode'] == "'manual'" and before['mode'] != now['mode']:
            settings('set', '', 'mode', before['mode'])
        path.unlink(missing_ok=True)
        return
    restore = record.get('restore')
    if restore is None:
        affected = [p for p in PROTOCOLS if owned(now[p], env['MIXED_PORT'])]
        restore = {}
        for protocol in affected:
            old = before[protocol] if before else {'host': "''", 'port': '0'}
            restore[protocol] = {'host': "''", 'port': '0'} if owned(old, env['MIXED_PORT']) else old
        if not restore:
            path.unlink(missing_ok=True)
            return
        # 在恢复任一字段前记录目标；恢复中断后可继续，不会因半写而丢失归属。
        record = {'before': before, 'restore': restore, 'phase': 'restoring'}
        private_write(path, json.dumps(record))
    for protocol, target in restore.items():
        allowed = {'host': {"'127.0.0.1'", target['host']},
                   'port': {env['MIXED_PORT'], target['port']}}
        if all(now[protocol][key] in allowed[key] for key in ('host', 'port')):
            for key in ('host', 'port'):
                if now[protocol][key] != target[key]:
                    settings('set', protocol, key, target[key])
    final = current()
    changed_by_other = any(final[p] != (restore[p] if p in restore else before[p]) for p in PROTOCOLS) if before else False
    foreign = any(final[p]['host'].strip("'") and int(final[p]['port']) > 0 for p in PROTOCOLS)
    if final['mode'] == "'manual'":
        if before and not changed_by_other:
            mode = before['mode']
            if mode == "'manual'" and not foreign:
                mode = "'none'"
            settings('set', '', 'mode', mode)
        elif not foreign:
            settings('set', '', 'mode', 'none')
    final = current()
    if final['mode'] == "'manual'" and any(owned(final[p], env['MIXED_PORT']) for p in PROTOCOLS):
        raise RuntimeError('仍有本工具的代理设置未恢复')
    path.unlink(missing_ok=True)


def resume(env):
    """登录/核心重启时恢复明确的开启意图，保留其他程序的手动/PAC设置。"""
    if not (Path(env['INSTALL_HOME']) / 'proxy-enabled').exists() or snapshot_path(env).exists():
        return
    if settings('get', '', 'mode') != "'none'":
        return
    for _ in range(20):
        pid = subprocess.run(['systemctl', '--user', 'show', env['UNIT_MIHOMO'], '-p', 'MainPID', '--value'],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        listeners = subprocess.run(['ss', '-tlnpH', 'sport = :' + env['MIXED_PORT']],
                                   capture_output=True, text=True, timeout=5).stdout
        if pid.isdigit() and int(pid) > 0 and f'pid={pid},' in listeners:
            start(env)
            return
        time.sleep(.2)
    raise RuntimeError('核心端口归属尚未确认，未自动接管系统代理')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'recover', 'resume', 'adopt'])
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    try:
        if not Path(args.state).is_file():
            return
        env = load(args.state)
        if env.get('SHADOW') == '1' or not shutil.which('gsettings'):
            return
        if args.action == 'adopt':
            now = current()
            if now['mode'] == "'manual'" and any(owned(now[p], env['MIXED_PORT']) for p in ('http', 'https')):
                private_write(Path(env['INSTALL_HOME']) / 'proxy-enabled', 'enabled\n')
        elif args.action == 'recover':
            active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', env['UNIT_MIHOMO']],
                                    capture_output=True, timeout=5).returncode == 0
            if active:
                resume(env)
                return
            stop(env, clear_intent=False)
        elif args.action == 'resume':
            resume(env)
        else:
            (start if args.action == 'start' else stop)(env)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'系统代理恢复/设置失败：{exc}\n')


if __name__ == '__main__':
    main()

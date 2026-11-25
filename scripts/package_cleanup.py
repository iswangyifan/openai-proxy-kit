"""包移除前按用户身份清理集成；root 不解析或执行用户配置。"""
import argparse
import fcntl
import json
import stat
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

from kit_state import load, REGISTRY, registrations
from system_proxy import stop as restore_proxy

SCRIPTS = Path(__file__).resolve().parent


def clean_user(keep_gui=False):
    records = registrations(os.getuid())
    registration = records[0] if records else None
    registered = {}
    if not os.environ.get('KIT_ENV') and Path.home() == Path(pwd.getpwuid(os.getuid()).pw_dir) and registration is not None:
        registered = json.loads(registration.read_text())
        os.environ['KIT_ENV'] = registered['state']
        os.environ['HOME'] = registered['home']
    location = Path.home() / '.local/share/openai-proxy-kit/state-location'
    default = location.read_text().strip() if location.is_file() else str(Path.home() / '.local/share/openai-proxy-kit/kit.env')
    state = Path(os.environ.get('KIT_ENV', default))
    os.environ['KIT_ENV'] = str(state)
    if not state.is_file():
        return
    lock = open(str(state) + '.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    env = load(state)
    child_env = dict(os.environ, OPK_LOCK_HELD=str(state))
    online = subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True, timeout=10).returncode == 0
    if online:
        subprocess.run(['bash', str(SCRIPTS / 'switch.sh'), 'full-stop'], env=child_env, check=True, timeout=90)
    else:
        restore_proxy(env)
    if not keep_gui:
        subprocess.run(['gapplication', 'action', 'io.github.opk.Console', 'quit'], capture_output=True, timeout=10)

    backup = Path(env['INSTALL_HOME']) / 'backup/desktop'
    if (backup / 'manifest.json').exists():
        subprocess.run([sys.executable, str(SCRIPTS / 'inject_desktop.py'), 'restore',
                        '--backup-dir', str(backup)], check=True, timeout=20)
    units_dir = Path.home() / '.config/systemd/user'
    for key in ('UNIT_GUARD_TIMER', 'UNIT_GUARD_SERVICE', 'UNIT_CONTROLLER', 'UNIT_MIHOMO'):
        unit = units_dir / env[key]
        if unit.is_file():
            if 'openai-proxy-kit' not in unit.read_text():
                raise RuntimeError(f'服务文件归属无法确认，保留：{unit.name}')
            unit.unlink()
        for wants in units_dir.glob('*.wants'):
            link = wants / unit.name
            if link.is_symlink() and link.resolve() == unit.resolve():
                link.unlink()
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=False, timeout=10)
    guard = Path.home() / '.local/bin/guard-system-proxy-kit.sh'
    if guard.is_file() and 'openai-proxy-kit' in guard.read_text():
        guard.unlink()
    for name in ('openai-proxy-kit-console.desktop', 'mihomo-panel.desktop'):
        path = Path.home() / '.local/share/applications' / name
        if path.is_file() and ('openai-proxy-kit' in path.read_text() or str(env['INSTALL_HOME']) in path.read_text()):
            path.unlink()
    # 保留订阅、配置、备份，但不再把已移除集成识别成已安装。
    state.rename(state.with_name('kit.env.removed-' + str(time.time_ns())))
    if registration is not None and registered.get('state') == str(state):
        registration.unlink(missing_ok=True)
    elif registration is not None:
        if json.loads(registration.read_text()).get('state') == str(state):
            registration.unlink()
    if location.is_file() and location.read_text().strip() == str(state):
        location.unlink()
    print('用户服务、启动器和代理已恢复；私有配置与备份已保留。')


def clean_all():
    if os.geteuid() != 0:
        raise RuntimeError('--all-users 仅供软件包管理器以 root 使用')
    failures = []
    accounts = {account.pw_uid: account for account in pwd.getpwall()}
    # NSS/域账号可能不在 getpwall 枚举内，补入当前已登录账号，不设 UID 上限。
    logged_in = subprocess.run(['loginctl', 'list-users', '--no-legend', '--no-pager'], capture_output=True, text=True, timeout=10)
    for line in logged_in.stdout.splitlines():
        try:
            uid = int(line.split()[0])
            accounts[uid] = pwd.getpwuid(uid)
        except (ValueError, IndexError, KeyError):
            continue
    registered_uids = set()
    for entry in registrations():
        uid = int(entry.name.split('-', 1)[0])
        try:
            accounts[uid] = pwd.getpwuid(uid)
            registered_uids.add(uid)
        except KeyError:
            raise RuntimeError(f'无法解析已登记账号 UID {uid}，请恢复账号解析后再移除')
    for account in accounts.values():
        # 仅探测存在性，用户状态始终在降权后的子进程里读取。
        marker = Path(account.pw_dir) / '.local/share/openai-proxy-kit/kit.env'
        if account.pw_uid not in registered_uids and not marker.is_file() and not marker.with_name('state-location').is_file():
            continue
        runtime = Path('/run/user') / str(account.pw_uid)
        command = ['runuser', '-u', account.pw_name, '--', 'env', '-i',
                   'HOME=' + account.pw_dir, 'USER=' + account.pw_name,
                   'PATH=/usr/bin:/bin', 'XDG_RUNTIME_DIR=' + str(runtime)]
        if (runtime / 'bus').exists():
            command += ['DBUS_SESSION_BUS_ADDRESS=unix:path=' + str(runtime / 'bus')]
        else:
            # 未登录用户也需要恢复持久化 dconf；使用独立 session bus。
            command += ['dbus-run-session', '--']
        command += ['/usr/bin/python3', str(Path(__file__).resolve()), '--user']
        result = subprocess.run(command, timeout=120)
        if result.returncode:
            failures.append(account.pw_name)
    if failures:
        raise RuntimeError('以下用户清理失败，已阻止移除软件：' + ', '.join(failures))


def main():
    parser = argparse.ArgumentParser(description='恢复用户集成，保留私有配置和备份')
    parser.add_argument('--all-users', action='store_true')
    parser.add_argument('--user', action='store_true')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--keep-gui', action='store_true')
    args = parser.parse_args()
    if not (args.all_users or args.user or args.yes):
        if input('恢复系统代理与启动器、移除用户服务，保留配置备份？[y/N] ').lower() != 'y':
            return
    try:
        if args.all_users:
            clean_all()
        else:
            clean_user(keep_gui=args.keep_gui)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'清理未完成，软件和配置应保留以便恢复：{exc}\n')


if __name__ == '__main__':
    main()

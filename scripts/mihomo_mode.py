"""以 config.yaml 为唯一持久真源切换 mihomo 的 rule/direct 模式。"""
import argparse
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

from kit_state import load as load_state, private_write
import websites

MODES = {'rule', 'direct'}


def config_path(env):
    return Path(env['MIHOMO_CONF_DIR']) / 'config.yaml'


def _checked_mode(value):
    mode = value.get('mode')
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('核心配置模式无效或不受支持（仅支持 rule/direct）')
    return mode


def saved_mode(env):
    path = config_path(env)
    if not path.is_file():
        return None
    return _checked_mode(websites.read_config(path))


def _runtime_mode(env):
    value = websites.api(env, '/configs')
    if not isinstance(value, dict):
        raise ValueError('核心运行模式响应无效')
    return _checked_mode(value)


def _render(original, config):
    header = []
    for line in original.splitlines(keepends=True):
        if not line.strip() or line.startswith('#'):
            header.append(line)
        else:
            break
    return ''.join(header) + yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def set_mode(env, mode):
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('仅支持 rule/direct 模式')
    path = config_path(env)
    if not path.is_file():
        raise ValueError('核心配置不存在，无法切换模式')
    original = path.read_text()
    config = websites.read_config(path)
    _checked_mode(config)
    config['mode'] = mode
    dns = config.get('dns')
    if dns is None:
        dns = config['dns'] = {}
    if not isinstance(dns, dict):
        raise ValueError('核心 DNS 配置格式无效')
    # direct 必须交给系统 resolver，否则公共 DoH 会绕过内网 DNS。
    dns['enable'] = mode == 'rule'
    rendered = _render(original, config)

    fd, filename = tempfile.mkstemp(prefix='.mode-', suffix='.yaml', dir=path.parent)
    temporary = Path(filename)
    os.close(fd)
    try:
        private_write(temporary, rendered)
        websites.validate_config(env, path, temporary)
    finally:
        temporary.unlink(missing_ok=True)

    live = websites.active(env)
    original_runtime = None
    if live:
        websites.assert_owner(env)
        original_runtime = _runtime_mode(env)

    disk_touched = False
    patch_attempted = False
    try:
        disk_touched = True
        private_write(path, rendered)
        # 核心可能在第一次采样后自动重启；不能只更新磁盘而跳过运行态。
        if not live and websites.active(env):
            websites.assert_owner(env)
            original_runtime = _runtime_mode(env)
            live = True
        if live:
            patch_attempted = True
            websites.api(env, '/configs', {'mode': mode}, method='PATCH')
            # PATCH 只切换 mode；force reload 才会应用当前磁盘的 DNS 开关。
            websites.reload_core(env, path)
            if _runtime_mode(env) != mode:
                raise RuntimeError('核心运行模式回读与目标不一致')
    except BaseException as error:
        incomplete = False
        if disk_touched:
            try:
                private_write(path, original)
                if path.read_text() != original:
                    incomplete = True
            except (OSError, ValueError):
                incomplete = True
        if patch_attempted:
            try:
                websites.api(env, '/configs', {'mode': original_runtime}, method='PATCH')
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                incomplete = True
            try:
                websites.reload_core(env, path)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                incomplete = True
            try:
                if _runtime_mode(env) != original_runtime:
                    incomplete = True
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                incomplete = True
        if incomplete:
            raise RuntimeError('模式切换失败，且原磁盘配置、运行模式或 DNS 配置恢复不完整；请检查核心服务后重试') from error
        raise


def main():
    parser = argparse.ArgumentParser(description='切换或读取 mihomo 持久模式')
    parser.add_argument('--state', required=True)
    parser.add_argument('action', choices=['get', 'set', 'verify'])
    parser.add_argument('mode', nargs='?', choices=sorted(MODES))
    args = parser.parse_args()
    if args.action in ('set', 'verify') and args.mode is None:
        parser.error('set/verify 需要 rule 或 direct')
    if args.action == 'get' and args.mode is not None:
        parser.error('get 不接受模式参数')
    os.umask(0o077)
    # GUI 超时首先发送 SIGTERM；转换为异常，让写盘事务执行恢复。
    def interrupted(*_):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        env = load_state(args.state)
        if args.action == 'get':
            print(saved_mode(env) or '')
        elif args.action == 'verify':
            websites.assert_owner(env)
            if saved_mode(env) != args.mode or _runtime_mode(env) != args.mode:
                raise RuntimeError('磁盘与核心运行模式未收敛到 ' + args.mode)
        else:
            set_mode(env, args.mode)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else '模式操作失败，请检查核心服务和私有配置'
        print(message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('模式操作已取消', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())

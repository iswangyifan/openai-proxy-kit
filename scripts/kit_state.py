"""安装状态是数据，不执行其中任何 shell 语句；兼容旧版单引号赋值。"""
import argparse
import json
import os
import re
import pwd
import shlex
import tempfile
import stat
from pathlib import Path

REGISTRY = Path('/var/lib/openai-proxy-kit/users')

KEYS = set(('STATE_VERSION INSTALL_HOME MIXED_PORT API_PORT DNS_PORT PROBE_PORT_BASE OPENAI_GROUP '
            'OPENAI_FILTER MAX_NODES MIHOMO_CONF_DIR MIHOMO_BIN MIHOMO_SECRET SUB_URL SHADOW '
            'GUARD_LOG UNIT_MIHOMO UNIT_CONTROLLER UNIT_GUARD_SERVICE UNIT_GUARD_TIMER '
            'MIHOMO_INSTALLED CONTROLLER_BIN CONTROLLER_ENV').split())


def registrations(uid=None):
    result = []
    if not REGISTRY.is_dir():
        return result
    for entry in REGISTRY.iterdir():
        prefix = entry.name.split('-', 1)[0]
        if not prefix.isdecimal():
            continue
        metadata = entry.lstat()
        owner = int(prefix)
        if stat.S_ISREG(metadata.st_mode) and metadata.st_uid == owner and (uid is None or uid == owner):
            result.append(entry)
    return result


def private_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path):
    result = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        lexer = shlex.shlex(line, posix=True, punctuation_chars=';&|<>')
        lexer.whitespace_split = True
        tokens = list(lexer)
        if len(tokens) != 1 or '=' not in tokens[0]:
            raise ValueError('状态文件包含非赋值内容，请重新配置')
        key, value = tokens[0].split('=', 1)
        if key not in KEYS or '\x00' in value or '\n' in value or '\r' in value:
            raise ValueError('状态文件包含不支持的字段或控制字符')
        if key.startswith('UNIT_') and not re.fullmatch(r'[A-Za-z0-9_.@:-]+\.(?:service|timer)', value):
            raise ValueError('无效的用户服务名称')
        result[key] = value
    return result


def quote(value):
    # 始终带引号，保留旧版文件的可读性；读取端仍只将其当数据解析。
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['shell', 'write', 'json', 'controller'])
    parser.add_argument('path')
    args = parser.parse_args()
    try:
        if args.action == 'controller':
            values = {'MIHOMO_API': 'http://127.0.0.1:' + os.environ['API_PORT'],
                      'MIHOMO_PROXY': 'http://127.0.0.1:' + os.environ['MIXED_PORT'],
                      'MIHOMO_SECRET': os.environ['MIHOMO_SECRET'],
                      'MIHOMO_UNIT': os.environ['UNIT_MIHOMO'],
                      'PROBE_PORT_BASE': os.environ.get('PROBE_PORT_BASE', '17900'),
                      'PROBE_MAP': str(Path(os.environ['MIHOMO_CONF_DIR']) / 'probe-map.json'),
                      'OPENAI_GROUP': os.environ['OPENAI_GROUP'],
                      'STATE_PATH': str(Path(os.environ['INSTALL_HOME']) / 'controller/route-state.json'),
                      'LOG_PATH': str(Path(os.environ['INSTALL_HOME']) / 'controller/route.log'),
                      'CURL_PATH': '/usr/bin/curl'}
            private_write(args.path, '# openai-proxy-kit managed EnvironmentFile\n' +
                          ''.join(k + '=' + json.dumps(v, ensure_ascii=False) + '\n' for k, v in values.items()))
        elif args.action == 'write':
            values = {k: os.environ[k] for k in sorted(KEYS) if k in os.environ}
            if any('\n' in v or '\r' in v for v in values.values()):
                raise ValueError('配置值不能包含换行')
            private_write(args.path, '# openai-proxy-kit managed state; data only\n' +
                          ''.join(f'{k}={quote(v)}\n' for k, v in values.items()))
            if values.get('SHADOW') != '1':
                private_write(Path.home() / '.local/share/openai-proxy-kit/state-location', str(Path(args.path).absolute()))
                if REGISTRY.is_dir() and Path.home() == Path(pwd.getpwuid(os.getuid()).pw_dir):
                    records = registrations(os.getuid())
                    if records:
                        registration = records[0]
                    else:
                        fd, filename = tempfile.mkstemp(prefix=str(os.getuid()) + '-', dir=REGISTRY)
                        os.close(fd)
                        registration = Path(filename)
                    private_write(registration, json.dumps({'state': str(Path(args.path).absolute()), 'home': str(Path.home())}))
                    for previous in records[1:]:
                        previous.unlink()
        else:
            values = load(args.path)
            print(json.dumps(values) if args.action == 'json' else
                  '\n'.join(f'{k}={quote(v)}' for k, v in values.items()))
    except (OSError, ValueError) as exc:
        parser.exit(1, f'无法读取或写入安装状态：{exc}\n')


if __name__ == '__main__':
    main()

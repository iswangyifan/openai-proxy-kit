"""从审核过的 lock 下载核心，并根据实际入包文件生成来源记录。"""
import gzip
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / 'packaging/core.lock.json').read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def origin(binary):
    actual = digest(binary)
    matches = [v for v in LOCK['assets'].values() if v['sha256'] == actual]
    if matches:
        return {'component': 'mihomo', 'version': LOCK['version'], 'source': LOCK['source'],
                'sha256': actual, 'license': 'GPL-3.0', 'download': matches[0]['url']}
    raise ValueError('发行包只接受 core.lock.json 中审核过的核心；新版本须同步更新二进制和源码锁定信息')


def fetch():
    arch = {'x86_64': 'amd64'}.get(platform.machine())
    if arch not in LOCK['assets']:
        raise ValueError('首版仅验证 amd64，其他架构须先增加受审查的 lock 与验收')
    asset = LOCK['assets'][arch]
    cache = ROOT / '.build-cache/core'
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / 'mihomo'
    if not binary.exists() or digest(binary) != asset['sha256']:
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            archive = Path(temporary) / 'core.gz'
            subprocess.run(['curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
                            '--retry', '2', '--max-time', '180', asset['url'], '-o', str(archive)], check=True)
            if digest(archive) != asset['archive_sha256']:
                raise ValueError('核心下载校验失败，拒绝解压或执行')
            candidate = Path(temporary) / 'mihomo'
            with gzip.open(archive, 'rb') as src, candidate.open('wb') as dst:
                shutil.copyfileobj(src, dst)
            if digest(candidate) != asset['sha256']:
                raise ValueError('核心解压校验失败，拒绝执行')
            candidate.chmod(0o755)
            os.replace(candidate, binary)
    (cache / 'ORIGIN.json').write_text(json.dumps(origin(binary), indent=2) + '\n')
    print('已验证固定核心：' + LOCK['version'])


def sources():
    filename = 'mihomo-source-' + LOCK['version'] + '.tar.gz'
    cache = ROOT / '.build-cache/core' / filename
    source = LOCK['source_archive']
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists() or digest(cache) != source['sha256']:
        subprocess.run(['curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
                        '--max-time', '180', source['url'], '-o', str(cache)], check=True)
    if digest(cache) != source['sha256']:
        raise ValueError('对应核心源码校验失败，不能生成发行产物')
    (ROOT / 'dist').mkdir(exist_ok=True)
    shutil.copyfile(cache, ROOT / 'dist' / filename)
    return filename


if __name__ == '__main__':
    try:
        if sys.argv[1] == 'fetch':
            fetch()
        elif sys.argv[1] == 'sources':
            print(sources())
        elif sys.argv[1] == 'origin':
            print(json.dumps(origin(sys.argv[2]), indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc))

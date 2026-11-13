"""按固定核心中的 Go buildinfo 收集许可与对应模块源码，校验 Go h1 后使用。"""
import base64
import concurrent.futures
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'third_party/mihomo-licenses'
CACHE = ROOT / '.build-cache/core/modules'


def buildinfo(binary):
    data = Path(binary).read_bytes()
    offset = data.index(b'\xff Go buildinf:')
    if not data[offset + 15] & 2:
        raise ValueError('需要 Go 1.18+ 内联 buildinfo')
    cursor = offset + 32
    def string():
        nonlocal cursor
        size = shift = 0
        while True:
            byte = data[cursor]; cursor += 1
            size |= (byte & 127) << shift
            if byte < 128:
                break
            shift += 7
        value = data[cursor:cursor + size]; cursor += size
        return value
    version = string().decode()
    text = string()[16:-16].decode()
    modules = []
    for line in text.splitlines():
        fields = line.split('\t')
        if fields[0] == 'dep':
            modules.append(dict(zip(('module', 'version', 'h1'), fields[1:4])))
        elif fields[0] == '=>':
            original = modules[-1]['module']
            modules[-1] = dict(zip(('module', 'version', 'h1'), fields[1:4]))
            modules[-1]['replaces'] = original
    return version, modules


def escape(value):
    return ''.join('!' + c.lower() if c.isupper() else c for c in value)


def download(url, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        partial = destination.with_name(destination.name + '.part')
        subprocess.run(['curl', '-fsSL', '--proto', '=https', '--proto-redir', '=https', '--retry', '2',
                        '--max-time', '120', url, '-o', str(partial)], check=True)
        os.replace(partial, destination)


def module_source(module):
    url = 'https://proxy.golang.org/' + escape(module['module']) + '/@v/' + escape(module['version']) + '.zip'
    filename = hashlib.sha256((module['module'] + '@' + module['version']).encode()).hexdigest() + '.zip'
    path = CACHE / filename
    download(url, path)
    with zipfile.ZipFile(path) as archive:
        digest = hashlib.sha256()
        for name in sorted(archive.namelist()):
            if '\n' in name:
                raise ValueError('模块 ZIP 文件名包含换行')
            digest.update((hashlib.sha256(archive.read(name)).hexdigest() + '  ' + name + '\n').encode())
        actual = 'h1:' + base64.b64encode(digest.digest()).decode()
        if actual != module.get('h1'):
            path.unlink(missing_ok=True)
            raise ValueError('模块源码 h1 校验失败：' + module['module'])
        return path, archive.namelist(), url


def prepare_module(item):
    index, module = item
    path, names, url = module_source(module)
    license_paths = []
    with zipfile.ZipFile(path) as archive:
        for name in names:
            filename = name.rsplit('/', 1)[-1]
            if re.match(r'(?i)^(licen[sc]e|copying|copyright|notice|authors)(?:$|[._-])', filename):
                relative = f'{index:03d}/' + name.split('/', 1)[-1]
                target = DIRECTORY / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                content = archive.read(name)
                target.write_bytes(content)
                license_paths.append({'path': relative, 'sha256': hashlib.sha256(content).hexdigest()})
    if not license_paths:
        raise ValueError('模块未找到许可文件，须人工处理：' + module['module'])
    return {**module, 'source_zip': url, 'licenses': license_paths, 'archive': path.name}


def prepare(binary):
    version, modules = buildinfo(binary)
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(prepare_module, enumerate(modules)))
    go_license = DIRECTORY / 'GO-LICENSE'
    download(f'https://raw.githubusercontent.com/golang/go/{version}/LICENSE', go_license)
    manifest = {'core_sha256': hashlib.sha256(Path(binary).read_bytes()).hexdigest(), 'go_version': version,
                'go_source': 'https://github.com/golang/go/tree/' + version,
                'go_license_sha256': hashlib.sha256(go_license.read_bytes()).hexdigest(), 'modules': records}
    (DIRECTORY / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    print(f'已校验并保存 {len(records)} 个实际 Go 模块的许可及源码来源')


def verify(binary):
    manifest = json.loads((DIRECTORY / 'manifest.json').read_text())
    version, modules = buildinfo(binary)
    assert manifest['core_sha256'] == hashlib.sha256(Path(binary).read_bytes()).hexdigest()
    assert manifest['go_version'] == version
    assert [{k: item[k] for k in module} for module, item in zip(modules, manifest['modules'])] == modules
    assert len(manifest['modules']) == len(modules)
    assert hashlib.sha256((DIRECTORY / 'GO-LICENSE').read_bytes()).hexdigest() == manifest['go_license_sha256']
    for module in manifest['modules']:
        for license in module['licenses']:
            assert hashlib.sha256((DIRECTORY / license['path']).read_bytes()).hexdigest() == license['sha256']
    return manifest


def sources(binary, output):
    manifest = verify(binary)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        archives = list(pool.map(module_source, manifest['modules']))
    with tarfile.open(output, 'w:gz') as archive:
        for module, (path, _, _) in zip(manifest['modules'], archives):
            archive.add(path, arcname='modules/' + path.name)
        archive.add(DIRECTORY / 'manifest.json', arcname='manifest.json')
    print('已附实际 Go 依赖源码包；ZIP 与清单中的 Go h1 对应')


if __name__ == '__main__':
    binary = os.environ.get('MIHOMO_BIN', str(ROOT / '.build-cache/core/mihomo'))
    action = sys.argv[1]
    if action == 'prepare':
        prepare(binary)
    elif action == 'verify':
        verify(binary)
    elif action == 'sources':
        sources(binary, sys.argv[2])

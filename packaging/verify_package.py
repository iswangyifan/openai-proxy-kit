"""验证真实发行包的布局、权限、移除钩子及二进制来源，不安装软件。"""
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
version = (ROOT / 'VERSION').read_text().strip()
package = ROOT / 'dist' / f'openai-proxy-kit_{version}_amd64.deb'
control = subprocess.check_output(['dpkg-deb', '--ctrl-tarfile', str(package)])
with tarfile.open(fileobj=io.BytesIO(control)) as archive:
    hook = archive.getmember('./prerm')
    assert hook.mode & 0o111
    assert b'package_cleanup.py --all-users' in archive.extractfile(hook).read()
with tempfile.TemporaryDirectory() as temporary:
    unpacked = Path(temporary)
    subprocess.run(['dpkg-deb', '--extract', str(package), temporary], check=True)
    app = unpacked / 'usr/lib/openai-proxy-kit'
    doc = unpacked / 'usr/share/doc/openai-proxy-kit'
    for name in ('kit_state.py', 'system_proxy.py', 'package_cleanup.py', 'runtime.py', 'gui.py', 'tray.py'):
        assert (app / 'scripts' / name).read_bytes() == (ROOT / 'scripts' / name).read_bytes(), name
    assert not (app / 'libexec/geodata').exists()
    assert not list(app.rglob('*.mjs'))
    assert not list(app.rglob('node_modules'))
    assert not list(app.rglob('kit.env'))
    assert (unpacked / 'var/lib/openai-proxy-kit/users').stat().st_mode & 0o7777 == 0o1777
    for name in ('LICENSE', 'NOTICE', 'MIHOMO-LICENSE', 'NODE-LICENSE', 'CONTROLLER-LICENSE', 'SECURITY.md'):
        assert (doc / name).is_file(), name
    origin = json.loads((doc / 'CORE-ORIGIN.json').read_text())
    assert origin['sha256'] == hashlib.sha256((app / 'libexec/mihomo').read_bytes()).hexdigest()
    components = json.loads((doc / 'MIHOMO-DEPENDENCIES/manifest.json').read_text())
    assert components['core_sha256'] == origin['sha256']
    for module in components['modules']:
        for license in module['licenses']:
            assert hashlib.sha256((doc / 'MIHOMO-DEPENDENCIES' / license['path']).read_bytes()).hexdigest() == license['sha256']
    assert origin == json.loads((ROOT / 'dist/CORE-ORIGIN.json').read_text())
    result = subprocess.run([str(app / 'libexec/openai-route-controller'), '--version'],
                            env={'PATH': '/nonexistent'}, capture_output=True, text=True, check=True)
    assert 'SEA=true' in result.stdout
    for file in unpacked.rglob('*'):
        if file.is_file():
            assert not file.stat().st_mode & 0o022, file
print('PASS: 发行包布局、私密配置排除、清理钩子、许可、实际核心来源及独立 controller')

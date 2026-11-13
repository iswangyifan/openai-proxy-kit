"""低误报的源码/历史凭据门禁；只打印位置，不回显匹配值。"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [re.compile(x) for x in (
    rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    rb'gh[pousr]_[A-Za-z0-9]{30,}', rb'github_pat_[A-Za-z0-9_]{50,}',
    rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}', rb'/home/(?:deepway|fangzhen)/')]
files = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
failures = []
for name in filter(None, files):
    path = ROOT / name
    if not path.is_file():
        continue
    if name.endswith('.env') or name.startswith(('.build-cache/', '.build-tools/', '.artifacts/', 'dist/', 'build/')):
        failures.append(name + ': forbidden generated/private file')
    data = path.read_bytes()
    if b'\0' not in data:
        for line, text in enumerate(data.splitlines(), 1):
            if any(p.search(text) for p in PATTERNS):
                failures.append(f'{name}:{line}: sensitive pattern')
# 检查拟发布的分支/tag历史；本地 AI 工具 notes 不属于发布内容。
objects = subprocess.check_output(['git', 'rev-list', '--objects', '--branches', '--tags', 'HEAD'], cwd=ROOT).decode().splitlines()
for entry in objects:
    oid, _, name = entry.partition(' ')
    if subprocess.check_output(['git', 'cat-file', '-t', oid], cwd=ROOT).strip() != b'blob':
        continue
    data = subprocess.check_output(['git', 'cat-file', 'blob', oid], cwd=ROOT)
    if b'\0' not in data and any(p.search(data) for p in PATTERNS):
        failures.append(f'history {oid[:12]} {name}: sensitive pattern')
if failures:
    raise SystemExit('\n'.join(failures))
print('源码与可达 Git 历史的凭据模式检查通过（不替代人工审查）')

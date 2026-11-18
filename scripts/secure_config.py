"""为旧安装补上认证，保留用户节点及规则。"""
import os
import sys
from pathlib import Path
import yaml
from kit_state import private_write
from route_model import write_probe_map
from websites import load_profile, merge_rules

path = Path(sys.argv[1])
original = path.read_text()
config = yaml.safe_load(original)
if os.environ.get('INSTALL_HOME'):
    settings = Path(os.environ['INSTALL_HOME']) / 'website-routing.json'
    if settings.with_name('website-routing.pending.json').exists():
        raise SystemExit('上次网站设置未完成，请先在代理网站页重新保存以恢复')
    if settings.is_file():
        profile = load_profile(settings)
        merge_rules(config, profile, os.environ.get('OPENAI_GROUP', 'OpenAI 自动选择'), profile)
config['secret'] = os.environ['MIHOMO_SECRET']
config['external-controller'] = '127.0.0.1:' + str(config.get('external-controller', '127.0.0.1:9090').rsplit(':', 1)[-1])
config['external-controller-cors'] = {'allow-origins': ['http://' + config['external-controller']], 'allow-private-network': False}
if '本文件由 openai-proxy-kit 自动生成' in original:
    config['rules'] = [r for r in config.get('rules', []) if not r.startswith(('GEOSITE,', 'GEOIP,'))]
    config['rules'] = ['MATCH,DIRECT' if r.startswith('MATCH,') else r for r in config['rules']]
    config['geo-auto-update'] = False
    config.pop('geox-url', None)
private_write(path.with_suffix('.yaml.pre-auth'), original)
private_write(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))

write_probe_map(config, path.parent / 'probe-map.json')

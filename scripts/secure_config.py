"""为旧安装补上认证，保留用户节点及规则。"""
import os
import sys
from pathlib import Path
import yaml
from kit_state import private_write

path = Path(sys.argv[1])
original = path.read_text()
config = yaml.safe_load(original)
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

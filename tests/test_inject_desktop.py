"""桌面启动器 no_proxy 合并与恢复回归；只操作临时目录。"""
import copy
import hashlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import inject_desktop


PROXY = 'http://127.0.0.1:17890'


class Args:
    proxy = PROXY


class InjectDesktopTests(unittest.TestCase):
    def test_active_legacy_upgrade_preserves_ownership_and_exact_restore(self):
        for mode in ('in_place', 'override', 'generated'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                apps = root / 'apps'
                apps.mkdir()
                target = apps / 'app.desktop'
                src = target if mode == 'in_place' else root / 'app.desktop'
                original = b'[Desktop Entry]\r\nExec=env NO_PROXY=original.corp KEEP=yes /usr/bin/app %U\r\n'
                if src != target:
                    src.write_text('[Desktop Entry]\nExec=/usr/bin/system-app\n')
                backup = root / 'backup'
                backup.mkdir()
                backup_path = backup / 'known-original'
                backup_path.write_bytes(original)
                legacy = (
                    inject_desktop.MARKER + '\nExec=env http_proxy=old '
                    'no_proxy=localhost NO_PROXY=localhost env NO_PROXY=original.corp /usr/bin/app %U\n'
                ).encode()
                target.write_bytes(legacy)
                record = {
                    'source': str(src), 'in_place': mode == 'in_place',
                    'restore_original': mode != 'generated', 'backup': str(backup_path),
                    'injected_sha256': hashlib.sha256(legacy).hexdigest(),
                }
                manifest = {'injected': {str(target): dict(record)}}
                with patch.dict(os.environ, {'no_proxy': 'upgrade.corp', 'NO_PROXY': '*.upgrade'}):
                    inject_desktop.do_inject(Args(), src, PROXY, apps, backup, manifest)
                rebuilt = target.read_bytes()
                self.assertNotEqual(rebuilt, legacy)
                line = next(line for line in target.read_text().splitlines() if line.startswith('Exec='))
                self.assertEqual(self.no_proxy_values(line), [
                    'localhost', '127.0.0.1', 'original.corp', 'upgrade.corp', '*.upgrade',
                ])
                self.assertIn('KEEP=yes /usr/bin/app %U', line)
                upgraded = manifest['injected'][str(target)]
                for key in ('source', 'in_place', 'restore_original', 'backup'):
                    self.assertEqual(upgraded[key], record[key])
                self.assertEqual(upgraded['injected_sha256'], hashlib.sha256(rebuilt).hexdigest())
                for _ in range(3):
                    with patch.dict(os.environ, {'no_proxy': 'later.corp', 'NO_PROXY': ''}):
                        # 直接选择用户覆盖也必须保留最初的 source/restore_original。
                        inject_desktop.do_inject(Args(), target, PROXY, apps, backup, manifest)
                        self.assertEqual(target.read_bytes(), rebuilt)
                        for key in ('source', 'in_place', 'restore_original', 'backup'):
                            self.assertEqual(manifest['injected'][str(target)][key], record[key])
                        inject_desktop.do_disable(apps, backup, manifest)
                        if mode == 'generated':
                            self.assertFalse(target.exists())
                        else:
                            self.assertEqual(target.read_bytes(), original)
                        inject_desktop.do_enable(Args(), apps, backup, manifest)
                    self.assertEqual(target.read_bytes(), rebuilt)
                    self.assertEqual(backup_path.read_bytes(), original)
                    self.assertEqual(list(backup.iterdir()), [backup_path])
                inject_desktop.do_restore(apps, backup, manifest)
                if mode == 'generated':
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(target.read_bytes(), original)
                self.assertEqual(manifest['injected'], {})

    def test_upgrade_refuses_unowned_unverified_missing_backup_and_manual_edits(self):
        for in_place in (False, True):
            for problem in ('unowned', 'missing_hash', 'missing_backup', 'marked_backup',
                            'edited', 'disabled_edited', 'disabled_missing_backup'):
                with self.subTest(in_place=in_place, problem=problem), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    apps = root / 'apps'
                    apps.mkdir()
                    target = apps / 'app.desktop'
                    src = target if in_place else root / 'app.desktop'
                    original = b'[Desktop Entry]\nExec=/usr/bin/app\n'
                    src.write_bytes(original)
                    backup = root / 'backup'
                    backup.mkdir()
                    backup_path = backup / 'original'
                    backup_path.write_bytes(original)
                    injected = (inject_desktop.MARKER + '\nExec=env http_proxy=old /usr/bin/app\n').encode()
                    record = {
                        'source': str(src), 'in_place': in_place, 'restore_original': True,
                        'backup': str(backup_path),
                        'injected_sha256': hashlib.sha256(injected).hexdigest(),
                    }
                    target.write_bytes(injected)
                    if problem == 'missing_hash':
                        del record['injected_sha256']
                    elif problem == 'missing_backup':
                        backup_path.unlink()
                    elif problem == 'edited':
                        target.write_bytes(injected + b'# user edit\n')
                    elif problem == 'disabled_edited':
                        target.write_bytes(original + b'# user edit\n')
                    elif problem == 'disabled_missing_backup':
                        target.write_bytes(original)
                        backup_path.unlink()
                    elif problem == 'marked_backup':
                        backup_path.write_bytes(injected)
                    manifest = {'injected': {} if problem == 'unowned' else {str(target): record}}
                    before_manifest = copy.deepcopy(manifest)
                    before_files = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                    with self.assertRaises(SystemExit):
                        inject_desktop.do_inject(Args(), src, PROXY, apps, backup, manifest)
                    self.assertEqual(manifest, before_manifest)
                    self.assertEqual({p: p.read_bytes() for p in root.rglob('*') if p.is_file()}, before_files)

    def no_proxy_values(self, line):
        pairs = re.findall(r'(?<!\S)(no_proxy|NO_PROXY)=([^\s]+)', line)
        self.assertGreaterEqual(len(pairs), 2, line)
        self.assertEqual([name for name, _ in pairs[:2]], ['no_proxy', 'NO_PROXY'])
        lower, upper = (value.split(',') for _, value in pairs[:2])
        self.assertEqual(lower, upper)
        self.assertNotIn('', lower)
        return lower

    def test_exec_merge_preserves_other_assignments_commands_and_actions(self):
        original = '''[Desktop Entry]
Exec=env no_proxy="original.corp,,10.1.2.3,original.corp" OTHER="two words" NO_PROXY='*.orig,10.0.0.0/8' MY_no_proxy=keep "/opt/My App" --title 'hello world' %F

[Desktop Action New]
Exec=env OTHER=action no_proxy=action.corp,localhost,, /usr/bin/app --set no_proxy=argument %U
Exec=/usr/bin/plain --literal NO_PROXY=argument
'''
        environment = {
            'no_proxy': 'first.corp,10.1.2.3,,192.0.2.0/24,*.internal',
            'NO_PROXY': 'FIRST.UPPER,10.1.2.3,2001:db8::/32,*.internal',
        }
        with patch.dict(os.environ, environment):
            result = inject_desktop.inject_text(original, PROXY)

        lines = [line for line in result.splitlines() if line.startswith('Exec=')]
        self.assertEqual(len(lines), 3)
        environment_items = [
            'first.corp', '192.0.2.0/24', '*.internal', 'FIRST.UPPER', '2001:db8::/32',
        ]
        self.assertEqual(self.no_proxy_values(lines[0]), [
            'localhost', '127.0.0.1', 'original.corp', '10.1.2.3', '*.orig', '10.0.0.0/8',
            *environment_items,
        ])
        self.assertEqual(self.no_proxy_values(lines[1]), [
            'localhost', '127.0.0.1', 'action.corp', 'first.corp', '10.1.2.3',
            *environment_items[1:],
        ])
        self.assertEqual(self.no_proxy_values(lines[2]), [
            'localhost', '127.0.0.1', 'first.corp', '10.1.2.3', *environment_items[1:],
        ])
        self.assertIn(
            'OTHER="two words" MY_no_proxy=keep "/opt/My App" --title \'hello world\' %F',
            lines[0],
        )
        self.assertIn('OTHER=action /usr/bin/app --set no_proxy=argument %U', lines[1])
        self.assertIn('/usr/bin/plain --literal NO_PROXY=argument', lines[2])
        self.assertNotIn('original.corp', lines[1])
        self.assertNotIn('action.corp', lines[0])

    def test_manifest_baseline_survives_environment_changes_and_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'apps/app.desktop'
            target.parent.mkdir()
            original = (
                b'[Desktop Entry]\r\n'
                b'Exec=env no_proxy=original.corp KEEP="original value" /usr/bin/app %F\r\n'
            )
            target.write_bytes(original)
            backup = root / 'backup'
            manifest = {'injected': {}}
            first_environment = {
                'no_proxy': 'first.corp,10.0.0.0/8',
                'NO_PROXY': '*.first,first.corp',
            }
            with patch.dict(os.environ, first_environment):
                inject_desktop.do_inject(Args(), target, PROXY, target.parent, backup, manifest)
            injected = target.read_bytes()
            record = manifest['injected'][str(target)]
            self.assertIn('first.corp', record['no_proxy_baseline'])
            inject_desktop.save_manifest(backup, manifest)

            for environment in (
                {'no_proxy': 'second.corp', 'NO_PROXY': 'SECOND.UPPER'},
                {'no_proxy': '', 'NO_PROXY': ''},
                first_environment,
            ):
                inject_desktop.do_disable(target.parent, backup, manifest)
                self.assertEqual(target.read_bytes(), original)
                manifest = inject_desktop.load_manifest(backup)
                with patch.dict(os.environ, environment):
                    inject_desktop.do_enable(Args(), target.parent, backup, manifest)
                self.assertEqual(target.read_bytes(), injected)
                inject_desktop.save_manifest(backup, manifest)
                values = self.no_proxy_values(next(
                    line for line in target.read_text().splitlines() if line.startswith('Exec=')
                ))
                self.assertIn('original.corp', values)
                self.assertIn('first.corp', values)
                self.assertIn('*.first', values)
                self.assertNotIn('second.corp', values)
                self.assertNotIn('SECOND.UPPER', values)

            inject_desktop.do_restore(target.parent, backup, manifest)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(manifest['injected'], {})

    def test_legacy_manifest_rebuilds_from_backup_and_current_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'apps/legacy.desktop'
            target.parent.mkdir()
            original = '[Desktop Entry]\nExec=env NO_PROXY=legacy.original KEEP=yes /usr/bin/legacy %U\n'
            backup = root / 'backup'
            backup.mkdir()
            backup_path = backup / 'legacy.desktop.orig'
            backup_path.write_text(original)
            old_injected = (
                '# openai-proxy-kit injected\n'
                'Exec=env http_proxy=http://127.0.0.1:7890 https_proxy=http://127.0.0.1:7890 '
                'HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 '
                'no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1 '
                'env NO_PROXY=legacy.original KEEP=yes /usr/bin/legacy %U\n'
            )
            target.write_text(old_injected)
            manifest = {'injected': {str(target): {
                'source': str(target),
                'in_place': True,
                'restore_original': True,
                'backup': str(backup_path),
                'injected_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            }}}

            inject_desktop.do_disable(target.parent, backup, manifest)
            self.assertEqual(target.read_text(), original)
            with patch.dict(os.environ, {
                'no_proxy': 'legacy.env,10.20.0.0/16',
                'NO_PROXY': '*.legacy,legacy.original',
            }):
                inject_desktop.do_enable(Args(), target.parent, backup, manifest)

            record = manifest['injected'][str(target)]
            self.assertIn('no_proxy_baseline', record)
            line = next(line for line in target.read_text().splitlines() if line.startswith('Exec='))
            values = self.no_proxy_values(line)
            for item in ('legacy.original', 'legacy.env', '10.20.0.0/16', '*.legacy'):
                self.assertIn(item, values)
            self.assertIn('KEEP=yes /usr/bin/legacy %U', line)
            self.assertNotIn('env NO_PROXY=legacy.original', line)

            rebuilt = target.read_bytes()
            inject_desktop.do_disable(target.parent, backup, manifest)
            with patch.dict(os.environ, {'no_proxy': 'later.env', 'NO_PROXY': 'later.env'}):
                inject_desktop.do_enable(Args(), target.parent, backup, manifest)
            self.assertEqual(target.read_bytes(), rebuilt)
            inject_desktop.do_restore(target.parent, backup, manifest)
            self.assertEqual(target.read_text(), original)


if __name__ == '__main__':
    unittest.main()

import io
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import runtime


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        ui = Path(self.directory.name) / 'ui'
        ui.mkdir()
        (ui / 'index.html').write_text('synthetic panel')
        for patcher in (
            patch.object(runtime, 'refresh_config'),
            patch.dict(runtime.ENV, {'MIHOMO_CONF_DIR': self.directory.name, 'API_PORT': '19097',
                                    'MIHOMO_SECRET': 'SYNTHETIC&?/# key'}, clear=True),
            patch.object(runtime, 'API_PORT', '19097'),
            patch.object(runtime, 'MIHOMO_API', 'http://127.0.0.1:19097'),
            patch.object(runtime, 'unit_active', return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_authenticates_and_uses_setup_fragment_for_old_browser_credentials(self):
        with patch.object(runtime.HTTP, 'open', return_value=io.BytesIO(b'{"version":"test"}')) as opened:
            url = runtime.panel_connection_url()
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.netloc, '127.0.0.1:19097')
        self.assertEqual(parsed.query, '')
        self.assertEqual(parsed.path, '/ui/')
        route, params = parsed.fragment.split('?', 1)
        self.assertEqual(route, '/setup')
        query = urllib.parse.parse_qs(params)
        self.assertEqual(query['secret'], ['SYNTHETIC&?/# key'])
        self.assertEqual(query['hostname'], ['127.0.0.1'])
        self.assertEqual(query['port'], ['19097'])
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:19097/version')
        self.assertEqual(request.get_header('Authorization'), 'Bearer SYNTHETIC&?/# key')
        self.assertNotIn('SYNTHETIC', runtime.redact(url))

    def test_rejects_authentication_errors_without_exposing_credentials(self):
        for code in (401, 403):
            error = urllib.error.HTTPError('http://127.0.0.1:19097/version', code, 'SYNTHETIC', {}, None)
            with patch.object(runtime.HTTP, 'open', side_effect=error):
                with self.assertRaisesRegex(RuntimeError, '本机管理连接验证失败') as caught:
                    runtime.panel_connection_url()
            self.assertNotIn('SYNTHETIC', str(caught.exception))

    def test_connection_failure_is_actionable(self):
        for error in (OSError('private message'), ValueError('private message')):
            with patch.object(runtime.HTTP, 'open', side_effect=error):
                with self.assertRaisesRegex(RuntimeError, '确认代理已开启'):
                    runtime.panel_connection_url()

    def test_stopped_core_does_not_open_browser(self):
        with patch.object(runtime, 'unit_active', return_value=False), patch.object(runtime.HTTP, 'open') as opened:
            with self.assertRaisesRegex(RuntimeError, '请先开启代理'):
                runtime.panel_connection_url()
            opened.assert_not_called()

    def test_missing_panel_does_not_return_a_broken_link(self):
        (Path(self.directory.name) / 'ui/index.html').unlink()
        with self.assertRaisesRegex(RuntimeError, '尚未安装高级管理页面'):
            runtime.panel_connection_url()


if __name__ == '__main__':
    unittest.main()

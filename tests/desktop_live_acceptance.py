"""临时虚拟机内，对已安装 GUI 的真实启停验收，不替换业务实现。"""
import os
from pathlib import Path
import socket
import sys
import threading
import time

if not Path('/etc/opk-acceptance-vm').exists() or not socket.gethostname().startswith('opk-acceptance'):
    raise SystemExit('Only run in the marked disposable acceptance VM')
sys.path.insert(0, '/usr/lib/openai-proxy-kit/scripts')
import gui
from gi.repository import Gio, GLib

app = gui.KitApp()
app.set_application_id('io.github.opk.LiveAcceptance')
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
errors, stage = [], ['initial']
fresh = '--fresh' in sys.argv
require_tray = '--require-tray' in sys.argv
deadline = time.monotonic() + (180 if fresh else 75)
server = None
if fresh:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Subscription(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write((Path.home() / 'accept/subscription.yaml').read_bytes())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Subscription)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def check():
    try:
        if time.monotonic() > deadline:
            raise AssertionError('live GUI acceptance timeout at ' + stage[0])
        if not app.window or app.busy or app.refreshing:
            return True
        status = app.status
        if stage[0] == 'initial' and require_tray and not app.tray.available:
            return True
        if stage[0] == 'initial' and fresh and not status.get('installed'):
            app.window.show_page('tools')
            assert not app.window.desktop_switch.get_active()
            app.window.subscription.set_text(f'http://127.0.0.1:{server.server_port}/subscription')
            app.window.configure_btn.emit('clicked')
            stage[0] = 'starting'
        elif stage[0] == 'initial' and status.get('installed') and status.get('phase') == 'off':
            assert app.window.power_btn.get_sensitive()
            app.window.power_btn.emit('clicked')
            stage[0] = 'starting'
        elif stage[0] == 'starting' and status.get('phase') == 'on':
            assert app.window.state_title.get_text() == '代理已开启'
            assert status.get('nodes')
            app.window.show_page('nodes')
            app.window.show_page('logs')
            app.window._render_logs()
            assert app.window.filtered_log_text()
            app.window.power_btn.emit('clicked')
            stage[0] = 'stopping'
        elif stage[0] == 'stopping' and status.get('phase') == 'off':
            assert app.window.state_title.get_text() == '代理已关闭'
            assert status.get('sys_proxy') == 'none'
            assert not status.get('running')
            app.quit()
            stage[0] = 'finished'
            return False
    except Exception as exc:
        errors.append(exc)
        app.quit()
        return False
    return True


GLib.timeout_add(250, check)
app.run([sys.argv[0]])
if server:
    server.shutdown()
    server.server_close()
if errors:
    raise errors[0]
assert stage[0] == 'finished'
assert app.follower.process is None
print('PASS: installed GUI real start/stop, nodes, logs and process cleanup')
if fresh:
    print('PASS: fresh GUI subscription configuration with default launcher opt-out')
if require_tray:
    print('PASS: registered tray with the real GNOME AppIndicator host')

"""实际 GTK 窗口与截图验证；固定离线状态，不触发代理或服务操作。"""
import ctypes
import ctypes.util
import os
import json
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(os.environ.get('OPK_TEST_APP_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(APP_ROOT / 'scripts'))
fixture_home = tempfile.TemporaryDirectory(prefix='opk-desktop-')
os.environ['HOME'] = fixture_home.name
os.environ['KIT_ENV'] = str(Path(fixture_home.name) / '.local/share/openai-proxy-kit/kit.env')
import gui
from gi.repository import Adw, Gio, GLib

gui.query_status = lambda: {
    'phase': 'off', 'running': False, 'installed': False, 'sys_proxy': 'none',
    'proxy_owned': False, 'services': {}, 'nodes': [], 'group_now': None,
    'injected': 0, 'checked_at': '离线测试',
}
app = gui.KitApp()
app.set_application_id('io.github.opk.DesktopTest')
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
app.tray.start = lambda: None
app.follower.start = lambda: None
errors = []
out = Path(__file__).resolve().parents[1] / '.artifacts'
out.mkdir(exist_ok=True)


def capture(name):
    # PyGObject 不能转换所有 GskRenderNode 子类，直接调用 GTK 渲染 API。
    paintable = app.paintable
    snapshot = gui.Gtk.Snapshot.new()
    paintable.snapshot(snapshot, app.window.get_width(), app.window.get_height())
    gtk = ctypes.CDLL(ctypes.util.find_library('gtk-4'))
    capsule_pointer = ctypes.pythonapi.PyCapsule_GetPointer
    capsule_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    capsule_pointer.restype = ctypes.c_void_p
    gtk.gtk_snapshot_to_node.argtypes = [ctypes.c_void_p]
    gtk.gtk_snapshot_to_node.restype = ctypes.c_void_p
    gtk.gsk_renderer_render_texture.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    gtk.gsk_renderer_render_texture.restype = ctypes.c_void_p
    gtk.gdk_texture_save_to_png.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    gtk.gdk_texture_save_to_png.restype = ctypes.c_int
    gtk.gsk_render_node_unref.argtypes = [ctypes.c_void_p]
    gtk.g_object_unref.argtypes = [ctypes.c_void_p]
    node = gtk.gtk_snapshot_to_node(capsule_pointer(snapshot.__gpointer__, None))
    assert node, '窗口必须已经渲染'
    texture = gtk.gsk_renderer_render_texture(
        capsule_pointer(app.window.get_renderer().__gpointer__, None), node, None)
    try:
        assert gtk.gdk_texture_save_to_png(texture, str(out / name).encode()), '截图写入失败'
    finally:
        gtk.g_object_unref(texture)
        gtk.gsk_render_node_unref(node)


def check():
    try:
        app.activate()
        if not hasattr(app, 'paintable'):
            app.paintable = gui.Gtk.WidgetPaintable.new(app.window)
            GLib.timeout_add(500, check)
            return False
        app.window.apply_status(gui.query_status())
        assert app.window.state_title.get_text() == '代理已关闭'
        assert not app.window.power_btn.get_sensitive()
        assert app.window.stack.get_child_by_name('logs') is not None
        app.window.apply_status(dict(gui.query_status(), phase='partial', installed=True,
            services={'mihomo': True}, api_available=False, issues=['节点读取失败：测试错误说明']))
        assert app.window.current_node.get_text() == '节点读取失败'
        assert '测试错误说明' in app.window.state_detail.get_text()
        app.window.apply_status(gui.query_status())
        capture('dashboard.png')
        app.logs.add('操作', '离线桌面测试：启动失败反馈', 'ERROR')
        app.logs.add('mihomo', '离线桌面测试：日志内容示例')
        app.window.show_page('logs')
        app.window.follow_switch.set_active(False)
        GLib.timeout_add(700, log_check)
    except Exception as exc:
        errors.append(exc)
        app.quit()
    return False


def log_check():
    try:
        app.window._render_logs()
        assert '启动失败反馈' in app.window.filtered_log_text()
        capture('logs.png')
        app.window.show_page('nodes')
        status = dict(gui.query_status(), nodes=['离线测试节点 A', '很长的离线测试节点名称 / 中文与 English'],
                      group_now='离线测试节点 A')
        app.window.apply_status(status)
        first = app.window.node_rows['离线测试节点 A'][0]
        app.window.apply_status(status)
        assert app.window.node_rows['离线测试节点 A'][0] is first, '状态刷新不应重建节点'
        app.style = Adw.StyleManager.get_default()
        app.style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        GLib.timeout_add(700, dark_check)
    except Exception as exc:
        errors.append(exc)
        app.quit()
    return False


def dark_check():
    try:
        capture('nodes-dark.png')
        app.window.show_page('tools')
        assert app.window.subscription.get_text() == ''
        gui.ENV['INSTALL_HOME'] = 'offline-fixture'
        app.window.apply_status(gui.query_status())
        assert not app.window.subscription.get_sensitive()
        assert app.window.configure_btn.get_child().get_last_child().get_text() == '升级内置控制器'
        gui.ENV.clear()
        gui.runtime.refresh_config()
        app.style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        app.window.show_page('websites')
        app.window.website_switches['github'].set_active(True)
        app.window.website_domains.get_buffer().set_text(' EXTRA.example. ')
        assert app.window.site_dirty
        app.window.website_save.emit('clicked')
        GLib.timeout_add(200, website_saved)
    except Exception as exc:
        errors.append(exc)
        app.quit()
    return False


def website_saved():
    if app.busy:
        return True
    try:
        profile = Path(fixture_home.name) / '.local/share/openai-proxy-kit/website-routing.json'
        assert profile.exists(), '保存按钮必须写入真实的私有设置文件'
        assert profile.stat().st_mode & 0o777 == 0o600
        value = json.loads(profile.read_text())
        assert value['enabled'] == ['github'] and value['domains'] == ['extra.example']
        assert not app.window.site_dirty
        assert not app.window.website_check.get_sensitive(), '配置订阅前不可检测'
        assert app.window.stack.get_visible_child_name() == 'websites'
        app.paintable = gui.Gtk.WidgetPaintable.new(app.window)
        app.window.set_default_size(1080, 740)
        app.window.present()
        app.window.queue_draw()
        GLib.timeout_add(400, website_capture)
    except Exception as exc:
        errors.append(exc)
        app.quit()
    return False


def website_capture():
    try:
        assert app.window.stack.get_visible_child_name() == 'websites'
        capture('websites.png')
        profile = Path(fixture_home.name) / '.local/share/openai-proxy-kit/website-routing.json'
        before = profile.read_bytes()
        app.window.website_domains.get_buffer().set_text('https://example.com/path')
        app.window.website_save.emit('clicked')
        assert '第 1 行' in app.window.sites_message.get_text()
        assert profile.read_bytes() == before, '无效输入不得覆盖已保存设置'
        app.window.reload_site_profile()
        active = dict(gui.query_status(), running=True, phase='on', installed=True, group_now='示例节点 A')
        gui.query_status = lambda: active
        app.status = active
        app._publish()
        gui.runtime.check_websites = lambda: {
            'openai': {'ok': True, 'detail': '公开端点可达'},
            'github': {'ok': False, 'detail': 'HTTP 403 或响应不符合预期'},
            'youtube': {'ok': None, 'detail': '预设未启用'}}
        app.window.website_check.emit('clicked')
        assert app.busy and not app.window.website_save.get_sensitive()
        GLib.timeout_add(100, website_detected)
    except Exception as exc:
        errors.append(exc)
        app.quit()
    return False


def website_detected():
    if app.busy:
        return True
    try:
        assert app.window.website_results['openai'].get_text() == '公开端点可达'
        assert '403' in app.window.website_results['github'].get_text()
        assert app.window.website_results['youtube'].get_text() == '预设未启用'
        assert app.window.website_save.get_sensitive()
    except Exception as exc:
        errors.append(exc)
    finally:
        app.quit()
    return False


GLib.timeout_add(1000, check)
GLib.timeout_add_seconds(15, lambda: (errors.append(RuntimeError('desktop timeout')), app.quit(), False)[-1])
app.run([sys.argv[0]])
if errors:
    raise errors[0]
assert app.follower.process is None
fixture_home.cleanup()
print('PASS: GTK 页面、日志、节点、网站真实保存及校验、深色模式和资源回收')

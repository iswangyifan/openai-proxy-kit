"""实际 GTK 窗口与截图验证；固定离线状态，不触发代理或服务操作。"""
import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

APP_ROOT = Path(os.environ.get('OPK_TEST_APP_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(APP_ROOT / 'scripts'))
os.environ['KIT_ENV'] = str(Path(__file__).resolve().parents[1] / '.artifacts/desktop-uninstalled.env')
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
print('PASS: GTK 页面、日志、深色模式、节点稳定更新和资源回收')

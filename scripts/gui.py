#!/usr/bin/env python3
"""openai-proxy-kit 桌面控制台（GTK4 + libadwaita）

还原 FlClash 概览页体验: 大电源开关 + 状态卡片 + 节点切换 + 延迟测试。
本脚本只是"壳"，开关逻辑唯一实现在 switch.sh，节点操作走 mihomo API。

依赖: python3-gi + GTK4 + libadwaita（GNOME 桌面默认自带）
用法: python3 gui.py   （或从应用菜单"代理控制台"启动）
"""

import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

INJECT_MARKER = "# openai-proxy-kit injected"


# ============================================================
# kit.env 读取（与 switch.sh 同构）
# ============================================================
def load_env() -> dict:
    kit_env = Path(os.environ.get(
        "KIT_ENV", os.path.expanduser("~/.local/share/openai-proxy-kit/kit.env")))
    env = {}
    if kit_env.is_file():
        for ln in kit_env.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Z_]+)='(.*)'\s*$", ln.strip())
            if m:
                env[m.group(1)] = m.group(2)
    return env


ENV = load_env()
MIXED_PORT = ENV.get("MIXED_PORT", "7890")
API_PORT = ENV.get("API_PORT", "9090")
OPENAI_GROUP = ENV.get("OPENAI_GROUP", "OpenAI 自动选择")
UNITS = {
    "mihomo": ENV.get("UNIT_MIHOMO", "mihomo-kit.service"),
    "controller": ENV.get("UNIT_CONTROLLER", "openai-route-controller-kit.service"),
    "guard": ENV.get("UNIT_GUARD_TIMER", "proxy-guard-kit.timer"),
}
SWITCH_BIN = os.environ.get(
    "OPK_SWITCH_BIN",
    str(Path(__file__).resolve().parent / "switch.sh"))
DESKTOP_BAK = Path(ENV.get("INSTALL_HOME",
                           os.path.expanduser("~/.local/share/openai-proxy-kit"))) / "backup/desktop"
MIHOMO_API = f"http://127.0.0.1:{API_PORT}"

UNIT_LABELS = {"mihomo": "mihomo 代理核心", "controller": "路由熔断器", "guard": "系统代理守护"}


# ============================================================
# 后台动作（一律在工作线程执行，结果经 GLib.idle_add 回主线程）
# ============================================================
def unit_active(unit: str) -> bool:
    try:
        return subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def sys_proxy_mode() -> str:
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.system.proxy", "mode"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip().strip("'")
    except Exception:
        return "unknown"


def count_injected() -> int:
    mf = DESKTOP_BAK / "manifest.json"
    if not mf.is_file():
        return 0
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        return sum(1 for p in manifest.get("injected", {})
                   if Path(p).is_file() and INJECT_MARKER in Path(p).read_text(encoding="utf-8"))
    except Exception:
        return 0


def mihomo_get(path: str, timeout: float = 4) -> dict | None:
    try:
        with urllib.request.urlopen(f"{MIHOMO_API}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def query_status() -> dict:
    services = {k: unit_active(u) for k, u in UNITS.items()}
    group = mihomo_get("/proxies/" + urllib.parse.quote(OPENAI_GROUP, safe=""))
    return {
        "services": services,
        "sys_proxy": sys_proxy_mode(),
        "injected": count_injected(),
        "running": services["mihomo"] and sys_proxy_mode() == "manual",
        "group_now": group.get("now") if group else None,
        "nodes": group.get("all", []) if group else [],
    }


def run_switch(on: bool):
    subprocess.run([SWITCH_BIN, "start" if on else "stop"],
                   capture_output=True, text=True, timeout=60)


def put_node(node: str) -> bool:
    req = urllib.request.Request(
        f"{MIHOMO_API}/proxies/" + urllib.parse.quote(OPENAI_GROUP, safe=""),
        method="PUT",
        data=json.dumps({"name": node}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 204
    except Exception:
        return False


def delay_test() -> dict:
    """对 OpenAI 组整体测延迟，返回 {节点名: 毫秒}（失败节点不在结果里）"""
    q = urllib.parse.urlencode({"url": "https://chatgpt.com", "timeout": 5000})
    data = mihomo_get("/group/" + urllib.parse.quote(OPENAI_GROUP, safe="") + f"/delay?{q}",
                      timeout=30)
    return data if isinstance(data, dict) else {}


CSS = b"""
.power-btn {
    border-radius: 9999px; min-width: 96px; min-height: 96px;
    background: @viewpoint_bg_color; color: @window_fg_color;
    font-size: 44px; padding: 0; border: none;
    transition: all 200ms ease;
}
.power-btn:hover { opacity: 0.9; }
.power-btn.on {
    background: @accent_bg_color; color: @accent_fg_color;
    box-shadow: 0 0 24px alpha(@accent_bg_color, 0.45);
}
.status-dot {
    border-radius: 9999px; min-width: 10px; min-height: 10px;
    background: @insensitive_fg_color;
}
.status-dot.on { background: @success_color; }
.dim-label { opacity: 0.65; }
.delay-chip { font-size: 11px; border-radius: 6px; padding: 1px 7px;
              background: alpha(@accent_bg_color, 0.15); }
"""


class MainWindow(Adw.PreferencesWindow):
    REFRESH_SEC = 3

    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("代理控制台")
        self.set_default_size(460, 640)
        self.switching = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._build_overview_page()
        self._build_nodes_page()
        self._refresh_async()

    # ---------- 概览页 ----------
    def _build_overview_page(self):
        page = Adw.PreferencesPage(title="概览", icon_name="go-home-symbolic")

        # 电源开关卡片
        grp = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      valign=Gtk.Align.CENTER, margin_top=18, margin_bottom=14)
        self.power_btn = Gtk.Button(css_classes=["power-btn"])
        self.power_btn.set_child(Gtk.Label(label="⏻"))
        self.power_btn.connect("clicked", self.on_power_clicked)
        self.power_state_lbl = Gtk.Label(css_classes=["dim-label"])
        box.append(self.power_btn)
        box.append(self.power_state_lbl)
        grp.add(box)
        page.add(grp)

        # 当前节点
        grp = Adw.PreferencesGroup(title="当前节点")
        self.node_row = Adw.ActionRow(title=OPENAI_GROUP, subtitle="—")
        open_panel = Gtk.Button(icon_name="external-link-symbolic",
                                tooltip_text="打开 metacubexd 管理面板")
        open_panel.connect("clicked", lambda *_: Gtk.UriLauncher.new(
            f"http://127.0.0.1:{API_PORT}/ui/").launch(self, None, None, None))
        self.node_row.add_suffix(open_panel)
        self.node_row.set_activatable(False)
        grp.add(self.node_row)
        page.add(grp)

        # 服务状态
        grp = Adw.PreferencesGroup(title="服务")
        self.svc_rows = {}
        self.svc_dots = {}
        for key in ("mihomo", "controller", "guard"):
            row = Adw.ActionRow(title=UNIT_LABELS[key])
            dot = Gtk.Box(css_classes=["status-dot"])
            row.add_suffix(dot)
            row.set_activatable(False)
            self.svc_rows[key] = row
            self.svc_dots[key] = dot
            grp.add(row)
        page.add(grp)

        # 系统层
        grp = Adw.PreferencesGroup(title="系统")
        self.proxy_row = Adw.ActionRow(title="GNOME 系统代理", subtitle="—")
        self.inject_row = Adw.ActionRow(title="桌面启动器注入", subtitle="—")
        for r in (self.proxy_row, self.inject_row):
            r.set_activatable(False)
            grp.add(r)
        page.add(grp)

        self.add(page)

    # ---------- 节点页 ----------
    def _build_nodes_page(self):
        self.nodes_page = Adw.PreferencesPage(title="节点", icon_name="network-server-symbolic")

        header = Adw.PreferencesGroup()
        hbox = Gtk.Box(spacing=10, halign=Gtk.Align.END)
        self.delay_btn = Gtk.Button(label="测试延迟")
        self.delay_btn.connect("clicked", self.on_delay_clicked)
        hbox.append(self.delay_btn)
        header.add(hbox)
        self.nodes_page.add(header)

        self.nodes_grp = Adw.PreferencesGroup(title=OPENAI_GROUP, description="点击切换节点")
        self.nodes_page.add(self.nodes_grp)
        self.nodes_grp_buf = self.nodes_grp  # 重建时替换
        self.delay_cache = {}
        self.add(self.nodes_page)

    # ---------- 状态刷新 ----------
    def _refresh_async(self):
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            st = query_status()
        except Exception as e:
            st = {"error": str(e)}
        GLib.idle_add(self._apply_status, st)
        GLib.timeout_add_seconds(self.REFRESH_SEC, self._refresh_tick)

    def _refresh_tick(self):
        """返回 False 停止本轮链式调度；动作执行期间挂起轮询"""
        if self.switching:
            return True
        threading.Thread(target=self._refresh_worker_once, daemon=True).start()
        return True

    def _refresh_worker_once(self):
        try:
            st = query_status()
        except Exception as e:
            st = {"error": str(e)}
        GLib.idle_add(self._apply_status, st)

    def _apply_status(self, st):
        if st.get("error"):
            self.power_state_lbl.set_text("状态查询失败")
            return False
        running = st["running"]
        self.power_btn.set_css_classes(["power-btn"] + (["on"] if running else []))
        self.power_state_lbl.set_text("运行中" if running else "已停止")
        self.node_row.set_subtitle(st["group_now"] or "—（mihomo 未运行）")
        for key in ("mihomo", "controller", "guard"):
            self.svc_dots[key].set_css_classes(
                ["status-dot"] + (["on"] if st["services"].get(key) else []))
        self.proxy_row.set_subtitle("manual → 127.0.0.1:" + MIXED_PORT
                                    if st["sys_proxy"] == "manual"
                                    else f"已关闭（{st['sys_proxy']}）")
        n = st["injected"]
        self.inject_row.set_subtitle(f"{n} 个应用生效" if n else "无")
        if st["nodes"]:
            self._rebuild_nodes(st["nodes"], st["group_now"])
        return False

    def _rebuild_nodes(self, nodes, now):
        grp = Adw.PreferencesGroup(title=OPENAI_GROUP, description="点击切换节点")
        for name in nodes:
            if name in ("DIRECT", "REJECT"):
                continue
            row = Adw.ActionRow(title=name)
            if name == now:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
                row.set_title(f"{name}  ✓")
            if name in self.delay_cache:
                d = self.delay_cache[name]
                chip = Gtk.Label(label=f"{d} ms", css_classes=["delay-chip"])
                row.add_suffix(chip)
            row.connect("activated", self.on_node_activated, name)
            grp.add(row)
        self.nodes_page.remove(self.nodes_grp_buf)
        self.nodes_grp_buf = grp
        self.nodes_page.add(grp)

    # ---------- 交互 ----------
    def on_power_clicked(self, *_):
        if self.switching:
            return
        target_on = "on" not in self.power_btn.get_css_classes()
        self.switching = True
        self.power_state_lbl.set_text("正在开启…" if target_on else "正在关闭…")
        self.power_btn.set_sensitive(False)

        def work():
            try:
                run_switch(target_on)
            finally:
                GLib.idle_add(self._after_switch)

        threading.Thread(target=work, daemon=True).start()

    def _after_switch(self):
        self.switching = False
        self.power_btn.set_sensitive(True)
        self._refresh_worker_once()
        return False

    def on_node_activated(self, _row, name):
        def work():
            put_node(name)
            GLib.idle_add(self._refresh_worker_once)
        threading.Thread(target=work, daemon=True).start()

    def on_delay_clicked(self, *_):
        self.delay_btn.set_sensitive(False)

        def work():
            self.delay_cache = delay_test()
            GLib.idle_add(self._after_delay)
        threading.Thread(target=work, daemon=True).start()

    def _after_delay(self):
        self.delay_btn.set_sensitive(True)
        self._refresh_worker_once()
        return False


class KitApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.openai-proxy-kit.console")

    def do_activate(self):
        win = self.props.active_window or MainWindow(self)
        win.present()


def main():
    if not Path(SWITCH_BIN).exists() and not os.environ.get("OPK_SWITCH_BIN"):
        print(f"[gui] 找不到 switch.sh: {SWITCH_BIN}", file=sys.stderr)
        sys.exit(1)
    app = KitApp()
    app.run(None)


if __name__ == "__main__":
    main()

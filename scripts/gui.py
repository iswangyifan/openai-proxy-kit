#!/usr/bin/env python3
"""GTK4 控制台：仪表盘、节点、实时日志、托盘及首次配置。"""
import os
import json
import sys
import threading
import runtime
from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from runtime import (ASSETS, ENV, ROOT, SWITCH_BIN,
                     UNITS, LogFollower, LogStore, command, delay_test, load_env,
                     mihomo_get, put_node, query_status, run_switch)
from tray import Tray

APP_ID = 'io.github.opk.Console'
STATE_LABELS = {'on': '代理已开启', 'off': '代理已关闭', 'partial': '代理需要检查', 'busy': '正在处理…'}


def label(text='', css=None, wrap=False):
    widget = Gtk.Label(label=text, xalign=0, wrap=wrap)
    widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if css:
        widget.add_css_class(css)
    return widget


def box(*children, vertical=True, spacing=12, **kwargs):
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                     spacing=spacing, **kwargs)
    for child in children:
        widget.append(child)
    return widget


def button(text, callback, icon=None, css='secondary'):
    widget = Gtk.Button(css_classes=[css])
    children = ([Gtk.Image(icon_name=icon)] if icon else []) + [Gtk.Label(label=text)]
    widget.set_child(box(*children, vertical=False, spacing=8, halign=Gtk.Align.CENTER))
    widget.connect('clicked', callback)
    return widget


def card(title, icon, *children, hero=False):
    heading = box(Gtk.Image(icon_name=icon), label(title, 'section-title'), vertical=False, spacing=9)
    return box(heading, *children, spacing=16, css_classes=['surface'] + (['hero'] if hero else []))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='openai-proxy-kit · 代理控制台')
        self.app = app
        self.set_default_size(1080, 740)
        self.add_css_class('opk')
        self.delay_cache, self.node_rows = {}, {}
        self.node_names = ()
        self.log_revision, self.log_filters = -1, None
        self.connect('close-request', self._close_requested)
        self.connect('notify::visible', self._visibility_changed)
        provider = Gtk.CssProvider()
        provider.load_from_path(str(ASSETS / 'style.css'))
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.IconTheme.get_for_display(self.get_display()).add_search_path(str(ASSETS / 'icons'))
        Gtk.Window.set_default_icon_name('openai-proxy-kit')
        self.style_manager = Adw.StyleManager.get_default()
        self.style_manager.connect('notify::dark', self._theme_changed)
        self._theme_changed()
        self.overlay = Adw.ToastOverlay()
        self.set_content(self.overlay)
        self.stack = Gtk.Stack(hexpand=True, vexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.stack.connect('notify::visible-child-name', self._page_changed)
        self.sidebar = self._sidebar()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label='代理控制台'))
        toggle = Gtk.ToggleButton(icon_name='sidebar-show-symbolic', active=True, tooltip_text='显示或隐藏导航')
        toggle.connect('toggled', lambda b: self.sidebar.set_visible(b.get_active()))
        header.pack_start(toggle)
        header.pack_end(button('查看日志', lambda *_: self.show_page('logs'), 'utilities-terminal-symbolic'))
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.overlay.set_child(box(self.sidebar, toolbar, vertical=False, spacing=0))
        self._overview_page()
        self._nodes_page()
        self._websites_page()
        self._logs_page()
        self._tools_page()
        self.show_page('overview')
        self.log_timer = GLib.timeout_add(400, self._render_logs)

    def _theme_changed(self, *_):
        if self.style_manager.get_dark():
            self.add_css_class('dark')
        else:
            self.remove_css_class('dark')

    def _sidebar(self):
        logo = Gtk.Image.new_from_file(str(ASSETS / 'icons/openai-proxy-kit.svg'))
        logo.set_pixel_size(38)
        brand = box(logo, box(label('Proxy Kit', 'brand'), label('Codex / ChatGPT', 'brand-note'), spacing=3),
                    vertical=False, spacing=10)
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, vexpand=True)
        self.pages = ['overview', 'nodes', 'websites', 'logs', 'tools']
        for name, icon in [('仪表盘', 'view-grid-symbolic'), ('代理节点', 'network-server-symbolic'),
                           ('代理网站', 'web-browser-symbolic'),
                           ('实时日志', 'utilities-terminal-symbolic'), ('工具与设置', 'preferences-system-symbolic')]:
            row = Gtk.ListBoxRow()
            row.set_child(box(Gtk.Image(icon_name=icon), label(name), vertical=False, spacing=12))
            self.nav.append(row)
        self.nav.connect('row-selected', lambda _n, row: self.stack.set_visible_child_name(
            self.pages[row.get_index()]) if row else None)
        self.tray_label = label('正在检测托盘支持', 'footer', True)
        sidebar = box(brand, self.nav, box(self.tray_label, label('AI 访问 · 内网直连切换', 'footer'), spacing=8),
                      spacing=28, css_classes=['sidebar'])
        sidebar.set_size_request(194, -1)
        return sidebar

    def _page(self, key, title, subtitle):
        content = box(label(title, 'page-title'), label(subtitle, 'page-subtitle', True), spacing=16,
                      css_classes=['content'])
        self.stack.add_named(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=content), key)
        return content

    def _overview_page(self):
        page = self._page('overview', '仪表盘', '代理状态、当前节点与桌面应用配置，一处查看。')
        self.state_pill = label('正在读取', 'pill')
        self.state_pill.set_halign(Gtk.Align.START)
        self.state_title = label('正在读取状态', 'state-title')
        self.state_detail = label('正在核对服务与系统代理配置。', 'detail', True)
        self.power_btn = button('开启代理', self.on_power_clicked, 'system-shutdown-symbolic', 'power')
        self.power_btn.set_halign(Gtk.Align.START)
        self.spinner = Gtk.Spinner()
        hero = card('连接控制', 'network-workgroup-symbolic', self.state_pill, self.state_title,
                    self.state_detail, box(self.power_btn, self.spinner, vertical=False), hero=True)
        hero.set_hexpand(True)
        self.proxy_value, self.proxy_detail = label('—', 'value', True), label('读取系统代理地址', 'detail', True)
        self.service_value, self.service_detail = label('—', 'value'), label('读取服务状态', 'detail', True)
        right = box(card('系统代理', 'network-transmit-receive-symbolic', self.proxy_value, self.proxy_detail),
                    card('后台服务', 'system-run-symbolic', self.service_value, self.service_detail))
        right.set_size_request(245, -1)
        page.append(box(hero, right, vertical=False, spacing=16))
        self.current_node, self.node_detail = label('尚未选择', 'value', True), label('启动代理后可查看候选节点', 'detail', True)
        node_card = card('当前节点', 'network-server-symbolic', self.current_node, self.node_detail,
                         button('管理节点', lambda *_: self.show_page('nodes'), 'go-next-symbolic'))
        self.apps_value = label('—', 'value')
        apps = card('应用代理配置', 'application-x-executable-symbolic', self.apps_value,
                    label('已运行的应用不会自动更新代理环境变量，必要时请完全退出后重新打开。', 'detail', True))
        grid = Gtk.Grid(column_spacing=16, row_spacing=16, column_homogeneous=True)
        grid.attach(node_card, 0, 0, 1, 1)
        grid.attach(apps, 1, 0, 1, 1)
        page.append(grid)
        page.append(box(Gtk.Image(icon_name='dialog-information-symbolic'),
            label('需要访问公司内网时，可关闭代理恢复直连。节点延迟不代表账号或业务请求一定可用。', wrap=True),
            vertical=False, css_classes=['notice']))
        self.checked = label('尚未刷新', 'detail')
        page.append(box(self.checked, button('刷新状态', lambda *_: self.app.refresh(), 'view-refresh-symbolic'), vertical=False))

    def _nodes_page(self):
        page = self._page('nodes', '代理节点', '查看当前选择；切换失败时可在实时日志中查看原因。')
        self.nodes_summary = label('启动代理后显示候选节点', 'detail', True)
        self.nodes_summary.set_hexpand(True)
        self.delay_btn = button('测试延迟', self.on_delay_clicked, 'network-transmit-symbolic')
        self.panel_btn = button('高级面板', self.open_panel, 'external-link-symbolic')
        self.panel_btn.set_tooltip_text('仅在当前部署已安装 metacubexd 时可用')
        page.append(box(self.nodes_summary, self.delay_btn, self.panel_btn, vertical=False))
        self.node_list = Gtk.ListBox(css_classes=['nodes'], selection_mode=Gtk.SelectionMode.NONE)
        self.node_list.connect('row-activated', self.on_node_activated)
        page.append(self.node_list)
        self.node_empty = label('暂无节点。请先开启代理；首次使用请到“工具与设置”配置订阅。', 'detail', True)
        page.append(self.node_empty)

    def _logs_page(self):
        page = self._page('logs', '实时日志', '服务日志与操作结果集中查看，复制内容自动隐藏 URL 和常见凭据字段。')
        self.log_search = Gtk.SearchEntry(placeholder_text='搜索日志内容', hexpand=True)
        self.log_source = Gtk.DropDown.new_from_strings(['全部来源', '操作', 'mihomo', 'controller', 'guard', '系统'])
        self.log_level = Gtk.DropDown.new_from_strings(['全部级别', 'INFO', 'WARN', 'ERROR'])
        page.append(box(self.log_search, self.log_source, self.log_level, vertical=False, spacing=8))
        self.follow_switch = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self.follow_switch.connect('notify::active', self._follow_changed)
        page.append(box(label('实时跟随'), self.follow_switch,
            button('复制', self.copy_logs, 'edit-copy-symbolic'), button('导出', self.export_logs, 'document-save-symbolic'),
            button('清空视图', self.clear_logs, 'edit-clear-symbolic'), vertical=False, spacing=10))
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=['log-view'])
        self.log_scroll = Gtk.ScrolledWindow(child=self.log_view, vexpand=True,
                                             min_content_height=320, hscrollbar_policy=Gtk.PolicyType.NEVER)
        page.append(self.log_scroll)
        page.append(label('仅保留最近 1200 条。离开日志页或隐藏窗口会停止读取；清空视图不删除系统日志。', 'detail', True))

    def _websites_page(self):
        page = self._page('websites', '代理网站', '选择哪些网站使用代理，其余流量保持直连。')
        self.site_dirty, self.site_loading, self.website_checked_node = False, False, None
        self.website_switches, self.website_results = {}, {}
        rows = []
        for key, title, detail in (
                ('openai', 'OpenAI / ChatGPT', '默认启用；自动选节点仍以 OpenAI 探测为依据。'),
                ('github', 'GitHub', '网页、API、代码资源、发布下载与 GitHub Pages。'),
                ('youtube', 'YouTube', '网页、视频和图片资源；不代理全部 Google 服务。')):
            result = label('未检测', 'detail', True)
            result.set_size_request(170, -1)
            self.website_results[key] = result
            if key == 'openai':
                control = label('始终启用', 'pill')
            else:
                control = Gtk.Switch(valign=Gtk.Align.CENTER)
                control.set_tooltip_text(title + ' 使用代理')
                control.update_property([Gtk.AccessibleProperty.LABEL], [title + ' 使用代理'])
                control.connect('notify::active', self._site_edited)
                self.website_switches[key] = control
            text = box(label(title, 'section-title'), label(detail, 'detail', True), spacing=5, hexpand=True)
            rows.append(box(text, result, control, vertical=False, spacing=16))
        page.append(card('网站分流', 'web-browser-symbolic', *rows,
            label('这些网站共用“代理节点”页中的当前节点。保存后只影响新连接，已经打开的页面可能需要重新加载。', 'detail', True)))
        self.website_domains = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True,
                                           top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
        self.website_domains.set_tooltip_text('每行一个公开域名，包含其子域；不含协议、路径、IP 或通配符')
        self.website_domains.update_property([Gtk.AccessibleProperty.LABEL], ['自定义代理域名，每行一个'])
        self.website_domains.get_buffer().connect('changed', self._site_edited)
        page.append(card('自定义域名', 'document-edit-symbolic',
            label('每行一个，例如 example.com。子域名会一并匹配；不要填写 https://、端口、路径或公司内网域名。', 'detail', True),
            Gtk.ScrolledWindow(child=self.website_domains, min_content_height=110,
                               hscrollbar_policy=Gtk.PolicyType.NEVER),
            label('设置保存在你的用户目录，更新订阅和升级软件后继续保留。', 'detail', True)))
        self.website_save = button('保存并应用', self.save_sites, 'document-save-symbolic', 'power')
        self.website_check = button('检测已启用网站', self.check_sites, 'network-transmit-symbolic')
        self.sites_message = label('', 'detail', True)
        footer = box(box(self.website_save, self.website_check, vertical=False, spacing=12),
            self.sites_message,
            label('检测只验证公开端点，不登录、不播放视频、不切换节点。自定义域名请自行访问验证。', 'detail', True),
            spacing=6, margin_start=28, margin_end=28, margin_top=10, margin_bottom=12)
        scroller = self.stack.get_child_by_name('websites')
        self.stack.remove(scroller)
        view = Adw.ToolbarView()
        view.set_content(scroller)
        view.add_bottom_bar(footer)
        self.stack.add_named(view, 'websites')
        self.reload_site_profile()

    def reload_site_profile(self):
        self.site_loading = True
        try:
            profile = runtime.website_profile()
            for key, control in self.website_switches.items():
                control.set_active(key in profile['enabled'])
            self.website_domains.get_buffer().set_text('\n'.join(profile['domains']))
            self.site_dirty = False
            self.sites_message.set_text('可先保存网站设置，完成订阅配置后生效。' if not ENV else '未勾选的预设不会自动添加域名；自定义域名独立生效。开启代理后可检测。')
        except (OSError, ValueError):
            self.sites_message.set_text('网站设置读取失败，请检查私有配置或备份后重试。')
        finally:
            self.site_loading = False

    def _site_edited(self, *_):
        if self.site_loading:
            return
        self.site_dirty = True
        self.website_check.set_sensitive(False)
        self.website_checked_node = None
        self.sites_message.set_text('修改尚未保存。保存后再检测网站。')
        for result in self.website_results.values():
            result.set_text('未检测')

    def save_sites(self, *_):
        from websites import normalize
        buffer = self.website_domains.get_buffer()
        domains = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        try:
            profile = normalize({'enabled': [k for k, c in self.website_switches.items() if c.get_active()],
                                 'domains': [line for line in domains.splitlines() if line.strip()]})
        except ValueError as exc:
            self.sites_message.set_text(str(exc))
            self.toast('请检查域名格式')
            return

        def finished(result):
            self.app.record_result(result, '保存代理网站')
            if result.returncode:
                self.sites_message.set_text('保存失败，请查看日志；恢复未完成时可重新保存以重试恢复。')
                return
            self.reload_site_profile()
            details = json.loads(result.stdout)
            self.sites_message.set_text('已保存并应用到当前代理。' if details['live'] else
                                       '已保存，开启代理后生效。' if details['configured'] else '已保存，完成订阅配置后生效。')
            self.website_checked_node = None
            for value in self.website_results.values():
                value.set_text('未检测')
        self.app.task(lambda: runtime.save_websites(profile), finished, '正在保存代理网站…')

    def check_sites(self, *_):
        def work():
            try:
                return runtime.check_websites()
            except Exception as exc:
                return {'error': runtime.redact(str(exc))}

        def finished(results):
            if 'error' in results:
                for value in self.website_results.values():
                    value.set_text('检测未完成')
                self.sites_message.set_text(results['error'])
                return
            for key, value in results.items():
                self.website_results[key].set_text(value['detail'])
            self.website_checked_node = self.app.status.get('group_now')
            self.sites_message.set_text('检测完成。公开端点可达不代表账号登录、完整下载或视频播放一定成功。')
        for key, value in self.website_results.items():
            value.set_text('检测中…' if key == 'openai' or self.website_switches[key].get_active() else '预设未启用')
        self.app.task(work, finished, '正在检测网站公开端点…')

    def _tools_page(self):
        page = self._page('tools', '工具与设置', '首次配置、链路诊断与窗口行为。')
        self.subscription = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True, placeholder_text='输入 Clash YAML 订阅地址')
        self.configure_btn = button('配置并开启代理', self.configure, 'network-connect-symbolic', 'power')
        self.setup_note = label('将创建用户服务、开启登录自启，并设置 GNOME HTTP/HTTPS 代理；关闭时恢复原设置。默认 OpenAI 走代理，可在“代理网站”中增加目标。', 'detail', True)
        page.append(card('代理配置', 'document-properties-symbolic', self.setup_note,
            self.subscription,
            label('请使用你有权管理的设备和网络，并遵守组织网络政策。本项目与 OpenAI 无隶属或背书关系。', 'detail', True),
            self.configure_btn))
        self.desktop_switch = Gtk.Switch(active=False, valign=Gtk.Align.CENTER)
        candidates = sorted(p.name for p in Path('/usr/share/applications').glob('*.desktop')
                            if any(x in p.name.lower() for x in ('code', 'chatgpt', 'cursor')))
        page.append(card('应用启动器适配（可选）', 'application-x-executable-symbolic',
            box(label('首次配置时适配下列应用'), self.desktop_switch, vertical=False),
            label('、'.join(candidates) or '未发现候选应用', 'detail', True),
            label('默认关闭；开启后备份用户启动器并加入代理变量，关闭代理会恢复。已运行应用需要重启。', 'detail', True)))
        self.diagnose_btn = button('运行链路诊断', self.diagnose, 'system-search-symbolic')
        page.append(card('诊断与排障', 'dialog-information-symbolic',
            label('检查服务、端口、节点与 OpenAI 连通性；结果写入实时日志。', 'detail', True), self.diagnose_btn,
            button('恢复用户集成', self.reset_integration, 'edit-undo-symbolic')))
        self.minimize_switch = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        page.append(card('窗口行为', 'preferences-desktop-display-symbolic',
            box(label('关闭窗口时保留托盘'), self.minimize_switch, vertical=False),
            label('托盘可用时关闭窗口将隐藏到托盘；托盘不可用时会明确询问退出方式。', 'detail', True),
            button('关闭代理并退出', lambda *_: self.app.stop_and_quit(), 'application-exit-symbolic')))

    def show_page(self, name):
        self.nav.select_row(self.nav.get_row_at_index(self.pages.index(name)))

    def _page_changed(self, *_):
        if (self.stack.get_visible_child_name() == 'websites' and hasattr(self, 'website_save')
                and not self.site_dirty and not self.app.busy):
            self.reload_site_profile()
        if hasattr(self, 'follow_switch'):
            self._follow_changed()

    def _visibility_changed(self, *_):
        if hasattr(self, 'follow_switch'):
            self._follow_changed()

    def _follow_changed(self, *_):
        if self.get_visible() and self.stack.get_visible_child_name() == 'logs' and self.follow_switch.get_active():
            self.app.follower.start()
        else:
            self.app.follower.stop()

    def _render_logs(self):
        if self.stack.get_visible_child_name() != 'logs' or not self.get_visible():
            return True
        filters = (self.log_source.get_selected_item().get_string(),
                   self.log_level.get_selected_item().get_string(), self.log_search.get_text())
        revision = self.app.logs.revision
        if (revision, filters) != (self.log_revision, self.log_filters):
            self.log_revision, self.log_filters = revision, filters
            text = '\n'.join(e.line() for e in self.app.logs.snapshot(*filters))
            self.log_view.get_buffer().set_text(text or '暂无匹配日志。开启实时跟随或执行一次操作后，日志会显示在这里。')
            if self.follow_switch.get_active():
                GLib.idle_add(self._scroll_logs)
        return True

    def _scroll_logs(self):
        adjustment = self.log_scroll.get_vadjustment()
        adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))
        return False

    def filtered_log_text(self):
        return '\n'.join(e.line() for e in self.app.logs.snapshot(
            self.log_source.get_selected_item().get_string(), self.log_level.get_selected_item().get_string(),
            self.log_search.get_text()))

    def copy_logs(self, *_):
        self.get_clipboard().set(self.filtered_log_text())
        self.toast('已复制脱敏日志')

    def export_logs(self, *_):
        dialog = Gtk.FileDialog(title='导出脱敏日志', initial_name='openai-proxy-kit.log')
        content = self.filtered_log_text()

        def saved(dlg, result):
            try:
                target = dlg.save_finish(result)
                target.replace_contents(content.encode(), None, False, Gio.FileCreateFlags.REPLACE_DESTINATION | Gio.FileCreateFlags.PRIVATE, None)
                self.toast('日志已导出')
            except GLib.Error as exc:
                if not exc.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                    self.toast('导出失败，请检查目标目录权限')
        dialog.save(self, None, saved)

    def clear_logs(self, *_):
        self.app.logs.clear()
        self.toast('已清空当前视图，系统日志保留')

    def toast(self, text):
        self.overlay.add_toast(Adw.Toast.new(text))

    def apply_status(self, status):
        phase = 'busy' if self.app.busy else status.get('phase', 'partial')
        self.state_title.set_text(self.app.busy_text if self.app.busy else STATE_LABELS[phase])
        self.state_pill.set_text('处理中' if self.app.busy else '已连接' if phase == 'on' else '需检查' if phase == 'partial'
                                else '直连模式' if status.get('sys_proxy') == 'none' else '未接管')
        self.state_pill.set_css_classes(['pill'] + (['success'] if phase == 'on' else ['warning'] if phase == 'partial' else []))
        self.state_detail.set_text('正在执行操作，请稍候。' if self.app.busy else
            '代理核心与系统代理已就绪，应用可能仍需重启。' if phase == 'on' else
            '；'.join(status.get('issues', [])) or '服务与系统代理状态不一致，请查看日志或运行诊断。' if phase == 'partial' else
            '系统代理未由本工具接管。需要使用 AI 服务时可开启代理。')
        self.power_btn.set_child(box(Gtk.Image(icon_name='system-shutdown-symbolic'),
            Gtk.Label(label='关闭代理' if status.get('phase') in ('on', 'partial') else '开启代理'), vertical=False, spacing=8))
        self.power_btn.set_sensitive(not self.app.busy and status.get('installed', False))
        self.spinner.set_spinning(self.app.busy)
        mode = status.get('sys_proxy', 'unknown')
        self.proxy_value.set_text('已开启' if status.get('proxy_owned') else '已关闭' if mode == 'none' else '其他配置')
        self.proxy_detail.set_text(f'127.0.0.1:{runtime.MIXED_PORT}' if status.get('proxy_owned') else
                                   '系统直连' if mode == 'none' else status.get('proxy_target') or mode)
        services = status.get('services', {})
        self.service_value.set_text('核心运行中' if services.get('mihomo') else '核心已停止')
        self.service_detail.set_text('自动切换：' + ('运行中' if services.get('controller') else '未运行') +
                                    '  ·  守护：' + ('运行中' if services.get('guard') else '未运行'))
        unreadable = services.get('mihomo') and status.get('api_available') is False
        self.current_node.set_text('节点读取失败' if unreadable else status.get('group_now') or '暂无节点')
        self.node_detail.set_text('管理 API 未返回节点列表，见上方状态说明' if unreadable else
                                  f"{len(status.get('nodes', []))} 个候选节点 · {runtime.OPENAI_GROUP}")
        self.apps_value.set_text(f"{status.get('injected', 0)} 个启动器已配置")
        self.checked.set_text('最近刷新 ' + status.get('checked_at', '—'))
        if not status.get('installed'):
            self.state_detail.set_text('首次使用请打开“工具与设置”，配置订阅后即可开启代理。')
        self.delay_btn.set_sensitive(not self.app.busy and bool(status.get('nodes')))
        self.configure_btn.set_sensitive(not self.app.busy)
        if ENV:
            self.configure_btn.get_child().get_last_child().set_text('升级内置控制器')
            self.subscription.set_sensitive(False)
            self.subscription.set_property('placeholder-text', '已有订阅配置；升级将复用现有配置')
        else:
            self.configure_btn.get_child().get_last_child().set_text('配置并开启代理')
            self.subscription.set_sensitive(True)
            self.subscription.set_property('placeholder-text', '输入 Clash YAML 订阅地址')
            self.setup_note.set_text('复用已有端口与服务名，切换到软件包内置的控制器。核心关闭时不会为了升级而开启代理。')
        self.diagnose_btn.set_sensitive(not self.app.busy)
        self.website_save.set_sensitive(not self.app.busy)
        self.website_check.set_sensitive(not self.app.busy and not self.site_dirty and status.get('running', False))
        self.website_domains.set_editable(not self.app.busy)
        for control in self.website_switches.values():
            control.set_sensitive(not self.app.busy)
        if self.website_checked_node and self.website_checked_node != status.get('group_now'):
            for value in self.website_results.values():
                value.set_text('节点已变更，请重新检测')
            self.website_checked_node = None
        ui = Path(ENV.get('MIHOMO_CONF_DIR', Path.home() / '.config/mihomo')) / 'ui'
        self.panel_btn.set_sensitive(bool(status.get('running')) and (ui / 'index.html').is_file())
        self._update_nodes(status)

    def _update_nodes(self, status):
        names = tuple(n for n in status.get('nodes', []) if n not in ('DIRECT', 'REJECT'))
        self.node_empty.set_visible(not names)
        self.nodes_summary.set_text(f"{len(names)} 个候选 · 当前 {status.get('group_now') or '未选择'}")
        if names != self.node_names:
            self.node_names = names
            while self.node_list.get_first_child():
                self.node_list.remove(self.node_list.get_first_child())
            self.node_rows.clear()
            for name in names:
                title = label(name)
                title.set_ellipsize(Pango.EllipsizeMode.END)
                title.set_hexpand(True)
                title.set_tooltip_text(name)
                chip = label('', 'pill')
                row = Gtk.ListBoxRow(activatable=True)
                row.node_name = name
                row.set_child(box(Gtk.Image(icon_name='network-server-symbolic'), title, chip, vertical=False))
                self.node_list.append(row)
                self.node_rows[name] = (row, chip)
        for name, (row, chip) in self.node_rows.items():
            current, delay = name == status.get('group_now'), self.delay_cache.get(name)
            chip.set_text(('当前 · ' if current else '') + (f'{delay} ms' if delay else '未测速'))
            chip.set_css_classes(['pill'] + (['success'] if current else []))
            row.set_sensitive(not self.app.busy)

    def on_power_clicked(self, *_):
        self.app.switch(self.app.status.get('phase') not in ('on', 'partial'))

    def on_node_activated(self, _list, row):
        def finished(ok):
            self.toast('节点已切换' if ok else '切换失败，请查看日志')
            self.app.logs.add('操作', '节点切换' + ('成功' if ok else '失败'), 'INFO' if ok else 'ERROR')
        self.app.task(lambda: put_node(row.node_name), finished, '正在切换节点…')

    def on_delay_clicked(self, *_):
        def finished(result):
            self.delay_cache = result
            self._update_nodes(self.app.status)
            self.toast('延迟测试完成' if result else '无可用测速结果，请查看服务状态')
        self.app.task(delay_test, finished, '正在测试延迟…')

    def open_panel(self, *_):
        Gtk.UriLauncher.new(f'http://127.0.0.1:{runtime.API_PORT}/ui/').launch(self, None, None, None)

    def configure(self, *_):
        if ENV:
            self.app.task(lambda: command(['bash', str(ROOT / 'install.sh'), 'controller'], timeout=30),
                          self._configured, '正在升级内置控制器…')
            return
        subscription = self.subscription.get_text().strip()
        if not subscription.startswith(('https://', 'http://')):
            self.toast('请输入 HTTP 或 HTTPS 格式的 Clash 订阅地址')
            return
        env = dict(os.environ, SUB_URL=subscription, ASSUME_YES='1',
                   DESKTOP_INJECT='1' if self.desktop_switch.get_active() else '0')
        self.subscription.set_text('')
        self.app.task(lambda: command(['bash', str(ROOT / 'install.sh')], timeout=300, env=env),
                      self._configured, '正在配置代理…')

    def _configured(self, result):
        self.app.record_result(result, '配置')
        if result.returncode == 0:
            ENV.update(load_env())
            runtime.refresh_config()
            self.toast('配置完成')

    def reset_integration(self, *_):
        if self.app.busy:
            return
        dialog = Adw.MessageDialog.new(self, '恢复用户集成', '将关闭本工具服务、恢复系统代理与应用启动器，保留订阅配置和备份。之后可重新配置。')
        dialog.add_response('cancel', '取消')
        dialog.add_response('restore', '恢复并保留配置')
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        def response(_dialog, selected):
            if selected != 'restore':
                return
            def finished(result):
                self.app.record_result(result, '恢复用户集成')
                if result.returncode == 0:
                    runtime.refresh_config()
                    self.toast('已恢复，私有配置和备份保留')
            self.app.task(lambda: command([sys.executable, str(ROOT / 'scripts/package_cleanup.py'), '--yes', '--keep-gui'], timeout=120), finished, '正在恢复用户集成…')
        dialog.connect('response', response)
        dialog.present()

    def diagnose(self, *_):
        self.show_page('logs')
        self.app.task(lambda: command(['bash', str(ROOT / 'diagnose.sh')], timeout=120),
                      lambda result: self.app.record_result(result, '诊断'), '正在运行诊断…')

    def tray_available(self, available):
        self.tray_label.set_text('托盘已就绪 · 关闭窗口可隐藏' if available else '当前桌面无可用托盘')
        if not available and not self.get_visible():
            self.present()

    def _close_requested(self, *_):
        if self.app.busy:
            self.toast('操作进行中，完成后再关闭窗口')
            return True
        if self.app.tray.available and self.minimize_switch.get_active():
            self.set_visible(False)
            return True
        dialog = Adw.MessageDialog.new(self, '关闭控制台', '可关闭代理后退出，或保留当前代理状态仅退出界面。')
        for key, text in [('cancel', '取消'), ('keep', '保留代理并退出'), ('stop', '关闭代理并退出')]:
            dialog.add_response(key, text)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.set_response_appearance('stop', Adw.ResponseAppearance.SUGGESTED)
        dialog.connect('response', lambda _d, response: self.app.stop_and_quit() if response == 'stop'
                       else self.app.quit() if response == 'keep' else None)
        dialog.present()
        return True


class KitApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option('logs', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, '打开实时日志', None)
        self.logs = LogStore()
        self.follower = LogFollower(self.logs)
        self.status = {}
        self.busy, self.refreshing, self.shutting_down = False, False, False
        self.busy_text = '正在处理…'
        self.window = None
        self.tray = Tray(self._tray_action, self._tray_available)

    def do_startup(self):
        Adw.Application.do_startup(self)
        quit_action = Gio.SimpleAction.new('quit', None)
        quit_action.connect('activate', lambda *_: self.quit())
        self.add_action(quit_action)
        self.hold()
        self.tray.start()
        self.poll = GLib.timeout_add_seconds(5, self.refresh)
        self.logs.add('操作', '控制台已启动；打开窗口不会自动开启代理')

    def do_command_line(self, cli):
        self.activate()
        if cli.get_options_dict().contains('logs'):
            self.window.show_page('logs')
        return 0

    def do_activate(self):
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()
        self.window.tray_available(self.tray.available)
        self.refresh()

    def _tray_action(self, action):
        if action in ('show', 'logs'):
            self.activate()
            if action == 'logs':
                self.window.show_page('logs')
        elif action == 'toggle':
            self.switch(self.status.get('phase') not in ('on', 'partial'))
        elif action == 'quit':
            self.stop_and_quit()

    def _tray_available(self, available):
        if self.window:
            self.window.tray_available(available)

    def refresh(self):
        if self.refreshing or self.busy or self.shutting_down:
            return True
        self.refreshing = True

        def worker():
            try:
                status = query_status()
            except Exception as exc:
                self.logs.add('系统', f'状态查询失败：{exc}', 'ERROR')
                status = dict(self.status, phase='partial', running=False)
            GLib.idle_add(self._status_ready, status)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _status_ready(self, status):
        self.refreshing = False
        if not self.shutting_down:
            self.status = status
            self._publish()
        return False

    def _publish(self):
        phase = 'busy' if self.busy else self.status.get('phase', 'off')
        self.tray.update(phase, self.busy_text if self.busy else STATE_LABELS[phase])
        if self.window:
            self.window.apply_status(self.status)

    def task(self, work, completed, title):
        if self.busy:
            return
        self.busy, self.busy_text = True, title
        self.logs.add('操作', title)
        self._publish()

        def worker():
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, str(exc)
            GLib.idle_add(finish, result, error)

        def finish(result, error):
            self.busy = False
            if error:
                self.logs.add('操作', error, 'ERROR')
                self.window.present()
                self.window.toast('操作失败，请查看日志')
            else:
                completed(result)
            self._publish()
            self.refresh()
            return False
        threading.Thread(target=worker, daemon=True).start()

    def record_result(self, result, action):
        for line in ((result.stdout or '') + '\n' + (result.stderr or '')).splitlines():
            if line.strip():
                self.logs.add('操作', line, 'INFO' if result.returncode == 0 else 'ERROR')
        text = action + ('完成' if result.returncode == 0 else f'失败（退出码 {result.returncode}）')
        self.logs.add('操作', text, 'INFO' if result.returncode == 0 else 'ERROR')
        if self.window:
            if result.returncode:
                self.window.present()
                self.window.show_page('logs')
            self.window.toast(text)

    def switch(self, on, quitting=False):
        def finished(result):
            self.record_result(result, '开启代理' if on else '关闭代理')
            if quitting and result.returncode == 0:
                self.task(query_status, verify_stopped, '正在确认关闭结果…')

        def verify_stopped(status):
            self.status = status
            if any(status.get('services', {}).values()) or status.get('proxy_owned'):
                self.window.present()
                self.window.toast('仍有服务或代理配置未关闭，请查看日志')
                self.logs.add('操作', '关闭后仍有残留服务或代理设置，已取消退出', 'ERROR')
            else:
                self.quit()
        self.task(lambda: run_switch(on), finished, '正在开启代理…' if on else '正在关闭代理…')

    def stop_and_quit(self):
        if self.busy:
            return
        if not ENV:
            self.quit()
        else:
            self.switch(False, quitting=True)

    def do_shutdown(self):
        self.shutting_down = True
        GLib.source_remove(self.poll)
        if self.window:
            GLib.source_remove(self.window.log_timer)
        self.follower.stop()
        self.tray.close()
        Adw.Application.do_shutdown(self)


def main():
    if not Path(SWITCH_BIN).exists():
        print(f'找不到启停脚本：{SWITCH_BIN}', file=sys.stderr)
        return 1
    return KitApp().run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())

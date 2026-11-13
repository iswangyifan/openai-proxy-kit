"""GTK4 托盘：通过 SNI / DBusMenu 导出，无 GTK3 或独立托盘进程。"""

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gio, GLib, GdkPixbuf

from runtime import ASSETS

ITEM_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

ITEM_XML = """<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/>
<property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="WindowId" type="u" access="read"/>
<property name="IconName" type="s" access="read"/>
<property name="IconPixmap" type="a(iiay)" access="read"/>
<property name="OverlayIconName" type="s" access="read"/>
<property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
<property name="AttentionIconName" type="s" access="read"/>
<property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
<property name="AttentionMovieName" type="s" access="read"/>
<property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<signal name="NewIcon"/><signal name="NewToolTip"/>
<signal name="NewStatus"><arg type="s"/></signal>
</interface></node>"""

MENU_XML = """<node><interface name="com.canonical.dbusmenu">
<property name="Version" type="u" access="read"/>
<property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="IconThemePath" type="as" access="read"/>
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/>
<arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
<arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/>
<arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/>
<arg type="v" direction="in"/><arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/>
<arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>"""


class Tray:
    def __init__(self, on_action, on_available):
        self.on_action = on_action
        self.on_available = on_available
        self.available = False
        self.phase = "off"
        self.summary = "代理已关闭"
        self.bus = None
        self.watch = 0
        self.subscriptions = []
        self.objects = []
        self.revision = 0
        self.pixmaps = {}
        self.closed = False

    def start(self):
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            for path, xml, method, prop in [(ITEM_PATH, ITEM_XML, self._item_method, self._item_property),
                                          (MENU_PATH, MENU_XML, self._menu_method, self._menu_property)]:
                info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
                self.objects.append(self.bus.register_object(path, info, method, prop, None))
            self.watch = Gio.bus_watch_name_on_connection(
                self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self._appeared, self._vanished)
        except GLib.Error:
            self.close()

    def _availability(self, available):
        if self.available != available:
            self.available = available
            self.on_available(available)

    def _appeared(self, bus, name, owner):
        self.owner = owner
        self.subscriptions.append(bus.signal_subscribe(owner, WATCHER, None,
            "/StatusNotifierWatcher", None, Gio.DBusSignalFlags.NONE,
            self._host_signal))
        self._check_host(owner)

    def _host_signal(self, _bus, _sender, _path, _interface, signal, _params):
        if signal in ("StatusNotifierHostRegistered", "StatusNotifierHostUnregistered"):
            self._check_host(self.owner)

    def _check_host(self, owner):
        if self.closed:
            return

        def checked(bus, result):
            try:
                if self.closed or owner != self.owner:
                    return
                value = bus.call_finish(result).unpack()[0]
                if not value:
                    self._availability(False)
                    return
                bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                         GLib.Variant("(s)", (bus.get_unique_name(),)), None,
                         Gio.DBusCallFlags.NONE, 2000, None, registered)
            except GLib.Error:
                self._availability(False)

        def registered(bus, result):
            try:
                bus.call_finish(result)
                if not self.closed and owner == self.owner:
                    self._availability(True)
            except GLib.Error:
                self._availability(False)

        self.bus.call(WATCHER, "/StatusNotifierWatcher", "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (WATCHER, "IsStatusNotifierHostRegistered")), None,
                      Gio.DBusCallFlags.NONE, 2000, None, checked)

    def _vanished(self, *_):
        self.owner = None
        for sub in self.subscriptions:
            self.bus.signal_unsubscribe(sub)
        self.subscriptions.clear()
        self._availability(False)

    def _pixmap(self):
        if self.phase not in self.pixmaps:
            path = ASSETS / "icons" / f"openai-proxy-kit-{self.phase}.svg"
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 48, 48, True)
            pixels = pixbuf.get_pixels()
            stride, channels = pixbuf.get_rowstride(), pixbuf.get_n_channels()
            argb = bytearray()
            for y in range(48):
                for x in range(48):
                    p = y * stride + x * channels
                    r, g, b = pixels[p:p + 3]
                    argb.extend((pixels[p + 3] if channels == 4 else 255, r, g, b))
            self.pixmaps[self.phase] = [(48, 48, bytes(argb))]
        return self.pixmaps[self.phase]

    def _item_property(self, _bus, _sender, _path, _interface, prop):
        strings = {"Category": "ApplicationStatus", "Id": "openai-proxy-kit",
                   "Title": "openai-proxy-kit", "Status": "Active", "IconName": "",
                   "AttentionIconName": "", "OverlayIconName": "", "AttentionMovieName": ""}
        if prop in strings:
            return GLib.Variant("s", strings[prop])
        if prop == "IconPixmap":
            return GLib.Variant("a(iiay)", self._pixmap())
        if prop in ("OverlayIconPixmap", "AttentionIconPixmap"):
            return GLib.Variant("a(iiay)", [])
        if prop == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", ("", self._pixmap(), "openai-proxy-kit", self.summary))
        return {"WindowId": GLib.Variant("u", 0), "ItemIsMenu": GLib.Variant("b", True),
                "Menu": GLib.Variant("o", MENU_PATH)}.get(prop)

    def _item_method(self, _bus, _sender, _path, _interface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate", "ContextMenu"):
            self.on_action("show")
        invocation.return_value(None)

    def items(self):
        busy = self.phase == "busy"
        return {1: (self.summary, False, None), 2: ("打开控制台", True, "show"),
                3: ("关闭代理" if self.phase in ("on", "partial") else "开启代理", not busy, "toggle"),
                4: ("查看实时日志", True, "logs"),
                5: ("关闭代理并退出", not busy, "quit")}

    def _properties(self, item):
        if item == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        label, enabled, _ = self.items()[item]
        return {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", enabled),
                "visible": GLib.Variant("b", True)}

    def _layout(self, item, depth, names):
        props = self._properties(item)
        if names:
            props = {k: v for k, v in props.items() if k in names}
        children = [GLib.Variant("(ia{sv}av)", self._layout(i, 0, names))
                    for i in self.items()] if item == 0 and depth != 0 else []
        return item, props, children

    def _menu_property(self, _bus, _sender, _path, _interface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(prop)

    def _event(self, item, event):
        if event == "clicked" and item in self.items():
            _, enabled, action = self.items()[item]
            if enabled and action:
                self.on_action(action)

    def _menu_method(self, _bus, _sender, _path, _interface, method, params, invocation):
        args = params.unpack()
        try:
            if method == "GetLayout":
                reply = GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(*args)))
            elif method == "GetGroupProperties":
                ids, names = args
                reply = GLib.Variant("(a(ia{sv}))", ([(i, {k: v for k, v in self._properties(i).items()
                    if not names or k in names}) for i in (ids or list(self.items()))],))
            elif method == "GetProperty":
                reply = GLib.Variant("(v)", (self._properties(args[0])[args[1]],))
            elif method == "Event":
                self._event(args[0], args[1])
                reply = None
            elif method == "EventGroup":
                for event in args[0]:
                    self._event(event[0], event[1])
                reply = GLib.Variant("(ai)", ([],))
            elif method == "AboutToShow":
                reply = GLib.Variant("(b)", (False,))
            elif method == "AboutToShowGroup":
                reply = GLib.Variant("(aiai)", ([], []))
            else:
                invocation.return_dbus_error("com.canonical.dbusmenu.UnknownMethod", method)
                return
            invocation.return_value(reply)
        except (KeyError, ValueError, TypeError):
            invocation.return_dbus_error("com.canonical.dbusmenu.InvalidArgument", "菜单项不存在")

    def update(self, phase, summary):
        if (phase, summary) == (self.phase, self.summary):
            return
        self.phase, self.summary = phase, summary
        self.revision += 1
        if self.bus and self.objects:
            for signal in ("NewIcon", "NewToolTip"):
                self.bus.emit_signal(None, ITEM_PATH, ITEM_IFACE, signal, None)
            self.bus.emit_signal(None, MENU_PATH, MENU_IFACE, "LayoutUpdated",
                                 GLib.Variant("(ui)", (self.revision, 0)))

    def close(self):
        self.closed = True
        if self.watch:
            Gio.bus_unwatch_name(self.watch)
        if self.bus:
            for sub in self.subscriptions:
                self.bus.signal_unsubscribe(sub)
            for obj in self.objects:
                self.bus.unregister_object(obj)
        self.objects.clear()
        self.subscriptions.clear()
        self.available = False

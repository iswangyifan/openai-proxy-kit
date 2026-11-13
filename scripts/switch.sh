#!/usr/bin/env bash
# switch.sh — openai-proxy-kit 代理链路一键开关
#
# 用法:
#   switch.sh start    # 一键开启: 服务 + 系统代理 + 桌面注入
#   switch.sh stop     # 一键关闭: 全部停用并取消自启，公司内网恢复直连
#   switch.sh status   # 查看各层状态
#
# 被 install.sh 的 start/stop/status 子命令和 Web 控制台 (console.py) 共用，
# 是开关逻辑的唯一实现源。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 安装态: 与 inject_desktop.py 同目录（install 时拷贝到 $INSTALL_HOME/scripts/）
# 仓库态: 与 common.sh 同目录（开发/测试时直接跑）
[ -f "$SCRIPT_DIR/common.sh" ] && . "$SCRIPT_DIR/common.sh" || true

KIT_ENV="${KIT_ENV:-$HOME/.local/share/openai-proxy-kit/kit.env}"
[ -f "$KIT_ENV" ] || die "未安装（找不到 $KIT_ENV），请先运行 ./install.sh"
# shellcheck disable=SC1090
. "$KIT_ENV"

SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
DESKTOP_BAK="$INSTALL_HOME/backup/desktop"
INJECT_PY="$SCRIPT_DIR/inject_desktop.py"
[ -f "$INJECT_PY" ] || INJECT_PY="$INSTALL_HOME/scripts/inject_desktop.py"

has_gsettings() { command -v gsettings >/dev/null 2>&1; }

unit_exists() { [ -f "$SYSTEMD_USER_DIR/$1" ]; }

# common.sh 缺位时的兜底（正常安装总在，防御式）
if ! command -v port_in_use >/dev/null 2>&1; then
    port_in_use() { ss -tlnH "sport = :$1" 2>/dev/null | grep -q .; }
fi

svc_start() {
    local u
    for u in "$UNIT_MIHOMO" "$UNIT_CONTROLLER" "$UNIT_GUARD_TIMER"; do
        if unit_exists "$u"; then
            systemctl --user enable --now "$u" 2>/dev/null || warn "启动失败: $u"
        else
            warn "unit 未安装，跳过: $u"
        fi
    done
    # 防断网: 确认 mihomo 端口监听后才允许动系统代理
    sleep 1
    if ! port_in_use "$MIXED_PORT"; then
        die "mihomo 端口 $MIXED_PORT 未监听（被 FlClash 等占用?）——已放弃开启系统代理，网络未受影响"
    fi
    # 系统代理指向 mihomo（仅 GNOME）
    if has_gsettings; then
        gsettings set org.gnome.system.proxy.http host '127.0.0.1'
        gsettings set org.gnome.system.proxy.http port "$MIXED_PORT"
        gsettings set org.gnome.system.proxy.https host '127.0.0.1'
        gsettings set org.gnome.system.proxy.https port "$MIXED_PORT"
        gsettings set org.gnome.system.proxy mode 'manual'
        ok "系统代理 → 127.0.0.1:$MIXED_PORT"
    fi
    # 桌面启动器注入恢复
    if [ -f "$DESKTOP_BAK/manifest.json" ] && [ -f "$INJECT_PY" ]; then
        python3 "$INJECT_PY" enable \
            --proxy "http://127.0.0.1:$MIXED_PORT" \
            --backup-dir "$DESKTOP_BAK" || warn "桌面注入恢复失败"
    fi
    ok "代理链路已开启（应用需从应用菜单重启才带代理）"
}

svc_stop() {
    # 必须先停 guard（否则每分钟把系统代理改回 manual），再停其余
    systemctl --user disable --now "$UNIT_GUARD_TIMER" "$UNIT_GUARD_SERVICE" 2>/dev/null || true
    systemctl --user disable --now "$UNIT_MIHOMO" "$UNIT_CONTROLLER" 2>/dev/null || true
    ok "服务已停止并取消自启"
    if has_gsettings; then
        gsettings set org.gnome.system.proxy mode 'none'
        ok "系统代理已关闭（mode=none）"
    fi
    if [ -f "$DESKTOP_BAK/manifest.json" ] && [ -f "$INJECT_PY" ]; then
        python3 "$INJECT_PY" disable --backup-dir "$DESKTOP_BAK" || warn "桌面注入停用失败"
    fi
    ok "代理链路已关闭（已开着的应用窗口重开后恢复直连）"
}

svc_status() {
    local u label
    for u in "$UNIT_MIHOMO" "$UNIT_CONTROLLER" "$UNIT_GUARD_TIMER"; do
        printf '  %-40s %s\n' "$u" "$(systemctl --user is-active "$u" 2>/dev/null || echo unknown)"
    done
    if has_gsettings; then
        printf '  %-40s %s\n' "gnome.system.proxy.mode" "$(gsettings get org.gnome.system.proxy mode 2>/dev/null)"
    fi
    if [ -f "$DESKTOP_BAK/manifest.json" ] && [ -f "$INJECT_PY" ]; then
        local inj
        inj=$(python3 -c "
import json,sys
from pathlib import Path
mf=json.loads(Path('$DESKTOP_BAK/manifest.json').read_text())
t=0
for p in mf.get('injected',{}):
    f=Path(p)
    if f.is_file() and '# openai-proxy-kit injected' in f.read_text():
        t+=1
print(t)")
        printf '  %-40s %s 个生效\n' "desktop-inject" "$inj"
    else
        printf '  %-40s %s\n' "desktop-inject" "未安装"
    fi
}

case "${1:-status}" in
    start)  svc_start ;;
    stop)   svc_stop ;;
    status) svc_status ;;
    *) die "用法: switch.sh {start|stop|status}" ;;
esac

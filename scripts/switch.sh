#!/usr/bin/env bash
# switch.sh — openai-proxy-kit 代理链路一键开关
#
# 用法:
#   switch.sh start    # 一键开启: 服务 + 系统代理 + 桌面注入
#   switch.sh stop     # 一键关闭: 全部停用并取消自启，公司内网恢复直连
#   switch.sh status   # 查看各层状态
#
# 被 install.sh 的 start/stop/status 子命令与 GTK 控制台共用，
# 是开关逻辑的唯一实现源。

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 安装态: 与 inject_desktop.py 同目录（install 时拷贝到 $INSTALL_HOME/scripts/）
# 仓库态: 与 common.sh 同目录（开发/测试时直接跑）
[ -f "$SCRIPT_DIR/common.sh" ] && . "$SCRIPT_DIR/common.sh" || true

KIT_ENV="${KIT_ENV:-$HOME/.local/share/openai-proxy-kit/kit.env}"
[ -f "$KIT_ENV" ] || die "未安装（找不到 $KIT_ENV），请先运行 ./install.sh"
# shellcheck disable=SC1090
lock_kit
load_kit_env || die "安装状态无效，请重新配置"

SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
DESKTOP_BAK="$INSTALL_HOME/backup/desktop"
INJECT_PY="$SCRIPT_DIR/inject_desktop.py"
[ -f "$INJECT_PY" ] || INJECT_PY="$INSTALL_HOME/scripts/inject_desktop.py"

has_gsettings() { command -v gsettings >/dev/null 2>&1; }

unit_exists() { [ -f "$SYSTEMD_USER_DIR/$1" ]; }
stop_unit() {
    if unit_exists "$1"; then
        systemctl --user disable --now "$1"
    elif systemctl --user is-active --quiet "$1"; then
        # unit 文件丢失时，systemd 仍可能缓存并运行该服务。
        systemctl --user stop "$1"
    fi
}


# common.sh 缺位时的兜底（正常安装总在，防御式）
if ! command -v port_in_use >/dev/null 2>&1; then
    port_in_use() { ss -tlnH "sport = :$1" 2>/dev/null | grep -q .; }
fi

svc_start() {
    local u
    if [ -z "${MIHOMO_SECRET:-}" ]; then
        bash "$SCRIPT_DIR/../install.sh" controller
        load_kit_env || return 1
    fi
    if port_in_use "$MIXED_PORT" && ! systemctl --user is-active --quiet "$UNIT_MIHOMO"; then
        die "端口 $MIXED_PORT 已被其他程序占用；请先退出其他代理或修改端口"
    fi
    unit_exists "$UNIT_MIHOMO" || die "核心服务未安装: $UNIT_MIHOMO"
    proxy_started=0
    trap 'code=$?; trap - EXIT; if [ "$code" != 0 ]; then if [ "$proxy_started" = 1 ]; then svc_stop || true; else systemctl --user disable --now "$UNIT_CONTROLLER" "$UNIT_MIHOMO" || true; fi; fi' EXIT
    trap 'exit 143' TERM
    trap 'exit 130' INT
    systemctl --user enable --now "$UNIT_MIHOMO"
    sleep 1
    local core_pid
    core_pid=$(systemctl --user show "$UNIT_MIHOMO" -p MainPID --value)
    if ! [[ "$core_pid" =~ ^[1-9][0-9]*$ ]] || \
       ! ss -tlnpH "sport = :$MIXED_PORT" 2>/dev/null | grep -Fq "pid=$core_pid,"; then
        err "无法确认端口 $MIXED_PORT 属于本工具核心，正在回滚"
        return 1
    fi
    for u in "$UNIT_CONTROLLER"; do
        if unit_exists "$u"; then
            systemctl --user enable --now "$u" 2>/dev/null || warn "启动失败: $u"
        else
            warn "unit 未安装，跳过: $u"
        fi
    done
    # 系统代理指向 mihomo（仅 GNOME）
    proxy_started=1
    python3 "$SCRIPT_DIR/system_proxy.py" start --state "$KIT_ENV"
    # 桌面启动器注入恢复
    if [ -f "$DESKTOP_BAK/manifest.json" ] && [ -f "$INJECT_PY" ]; then
        python3 "$INJECT_PY" enable \
            --proxy "http://127.0.0.1:$MIXED_PORT" \
            --backup-dir "$DESKTOP_BAK" || { err "桌面注入恢复失败"; return 1; }
    fi
    if unit_exists "$UNIT_GUARD_TIMER"; then
        systemctl --user enable --now "$UNIT_GUARD_TIMER" || { err "启动守护失败: $UNIT_GUARD_TIMER"; return 1; }
    fi
    trap - EXIT TERM INT
    ok "代理链路已开启（应用需从应用菜单重启才带代理）"
}

svc_stop() {
    local u failed=0
    for u in "$UNIT_GUARD_TIMER" "$UNIT_GUARD_SERVICE"; do
        stop_unit "$u" || { err "关闭失败: $u"; return 1; }
    done
    # 恢复失败时保留核心，避免产生死代理。
    python3 "$SCRIPT_DIR/system_proxy.py" stop --state "$KIT_ENV" || return 1
    if [ -f "$DESKTOP_BAK/manifest.json" ] && [ -f "$INJECT_PY" ]; then
        python3 "$INJECT_PY" disable --backup-dir "$DESKTOP_BAK" || { err "桌面注入停用失败，核心保留以便恢复"; return 1; }
    fi
    # 守护与启动器已清理，再停止核心，避免死代理
    for u in "$UNIT_CONTROLLER" "$UNIT_MIHOMO"; do
        stop_unit "$u" || { err "停止失败: $u"; failed=1; }
    done
    [ "$failed" = 0 ] || die "部分组件关闭失败，请查看日志或运行诊断"
    ok "代理链路已关闭（系统代理和启动器已恢复；运行中应用需重开）"
}

svc_status() {
    local u
    for u in "$UNIT_MIHOMO" "$UNIT_CONTROLLER" "$UNIT_GUARD_TIMER"; do
        printf '  %-40s %s\n' "$u" "$(systemctl --user is-active "$u" 2>/dev/null || true)"
    done
    if has_gsettings; then
        printf '  %-40s %s\n' "gnome.system.proxy.mode" "$(gsettings get org.gnome.system.proxy mode 2>/dev/null)"
    fi
    if [ -f "$DESKTOP_BAK/manifest.json" ] && [ -f "$INJECT_PY" ]; then
        local inj
        inj=$(python3 -c "
import json,sys
from pathlib import Path
mf=json.loads(Path(sys.argv[1]).read_text())
t=0
for p in mf.get('injected',{}):
    f=Path(p)
    if f.is_file() and '# openai-proxy-kit injected' in f.read_text():
        t+=1
print(t)" "$DESKTOP_BAK/manifest.json")
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

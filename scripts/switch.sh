#!/usr/bin/env bash
# switch.sh — openai-proxy-kit 代理链路一键开关
#
# 用法:
#   switch.sh start      # 恢复 rule 代理链路、系统代理和桌面注入
#   switch.sh stop       # 切换 direct 常驻，退出规则代理组件并恢复系统设置
#   switch.sh full-stop  # 彻底停止并禁用全部组件，供维护或卸载使用
#   switch.sh status     # 查看各层状态
#
# 被 install.sh 的启停子命令与 GTK 控制台共用，是开关逻辑的唯一实现源。

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 安装态: 与其他脚本同目录；仓库态: 与 common.sh 同目录。
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
MODE_PY="$SCRIPT_DIR/mihomo_mode.py"
[ -f "$MODE_PY" ] || MODE_PY="$INSTALL_HOME/scripts/mihomo_mode.py"

has_gsettings() { command -v gsettings >/dev/null 2>&1; }
unit_exists() { [ -f "$SYSTEMD_USER_DIR/$1" ]; }
stop_unit() {
    if unit_exists "$1"; then
        if [ "$(systemctl --user is-enabled "$1" 2>/dev/null || true)" = static ]; then
            systemctl --user stop "$1"
        else
            systemctl --user disable --now "$1"
        fi
    elif systemctl --user is-active --quiet "$1"; then
        # unit 文件丢失时，systemd 仍可能缓存并运行该服务。
        systemctl --user stop "$1"
    fi
}

# common.sh 缺位时的兜底（正常安装总在，防御式）。
if ! command -v port_in_use >/dev/null 2>&1; then
    port_in_use() { ss -tlnH "sport = :$1" 2>/dev/null | grep -q .; }
fi

mode_get() { python3 "$MODE_PY" --state "$KIT_ENV" get; }
mode_set() { python3 "$MODE_PY" --state "$KIT_ENV" set "$1"; }

assert_core_owner() {
    local core_pid port
    systemctl --user is-active --quiet "$UNIT_MIHOMO" || return 1
    core_pid=$(systemctl --user show "$UNIT_MIHOMO" -p MainPID --value 2>/dev/null) || return 1
    [[ "$core_pid" =~ ^[1-9][0-9]*$ ]] || return 1
    for port in "$MIXED_PORT" "$API_PORT"; do
        ss -tlnpH "sport = :$port" 2>/dev/null | grep -Fq "pid=$core_pid," || return 1
    done
}

assert_cold_ports_free() {
    local port
    for port in "$MIXED_PORT" "$API_PORT"; do
        if port_in_use "$port"; then
            err "端口 $port 已被其他程序占用；请先退出其他代理或修改端口"
            return 1
        fi
    done
}

ensure_direct_core() {
    unit_exists "$UNIT_MIHOMO" || { err "核心服务未安装: $UNIT_MIHOMO"; return 1; }
    if systemctl --user is-active --quiet "$UNIT_MIHOMO"; then
        assert_core_owner || { err "无法确认管理及代理端口属于本工具核心，未切换 direct"; return 1; }
    else
        assert_cold_ports_free || return 1
    fi
    mode_set direct || { err "无法将磁盘和运行模式切换为 direct，代理组件保持不变"; return 1; }
    systemctl --user enable --now "$UNIT_MIHOMO" || { err "无法保持核心 direct 常驻"; return 1; }
    sleep 1
    assert_core_owner || { err "无法确认 direct 核心及端口归属"; return 1; }
    # 冷启动/自动重启可能跨过首次 active 采样，最终必须核对运行模式。
    if ! python3 "$MODE_PY" --state "$KIT_ENV" verify direct; then
        mode_set direct || return 1
        python3 "$MODE_PY" --state "$KIT_ENV" verify direct || return 1
    fi
}

svc_start() {
    if [ -z "${MIHOMO_SECRET:-}" ]; then
        bash "$SCRIPT_DIR/../install.sh" controller
        load_kit_env || return 1
    fi
    unit_exists "$UNIT_MIHOMO" || die "核心服务未安装: $UNIT_MIHOMO"

    START_COLD=0
    if systemctl --user is-active --quiet "$UNIT_MIHOMO"; then
        assert_core_owner || die "无法确认管理及代理端口属于本工具核心，未开启代理链路"
        mode_get >/dev/null || die "核心配置模式无效，未开启代理链路"
    else
        START_COLD=1
        assert_cold_ports_free || return 1
        # 冷启动仅以 direct 接管端口，完整规则链路就绪前不启用 rule。
        mode_set direct || die "无法准备 direct 冷启动配置"
    fi

    trap 'code=$?; trap - EXIT TERM INT; if [ "$code" != 0 ]; then if [ "$START_COLD" = 1 ]; then svc_full_stop || err "冷启动失败后的彻底清理未完成"; else svc_stop || err "direct 热启动失败后的回滚未完成"; fi; fi; exit "$code"' EXIT
    trap 'exit 143' TERM
    trap 'exit 130' INT
    systemctl --user enable --now "$UNIT_MIHOMO" || { err "启动失败: $UNIT_MIHOMO"; return 1; }
    sleep 1
    assert_core_owner || { err "无法确认端口属于本工具核心，正在回滚"; return 1; }
    mode_set rule || { err "恢复 rule 模式失败"; return 1; }

    if unit_exists "$UNIT_CONTROLLER"; then
        systemctl --user enable --now "$UNIT_CONTROLLER" 2>/dev/null || { err "启动失败: $UNIT_CONTROLLER"; return 1; }
    else
        warn "unit 未安装，跳过: $UNIT_CONTROLLER"
    fi
    python3 "$SCRIPT_DIR/system_proxy.py" start --state "$KIT_ENV"
    if [ -f "$DESKTOP_BAK/manifest.json" ] && [ -f "$INJECT_PY" ]; then
        python3 "$INJECT_PY" enable \
            --proxy "http://127.0.0.1:$MIXED_PORT" \
            --backup-dir "$DESKTOP_BAK" || { err "桌面注入恢复失败"; return 1; }
    fi
    if unit_exists "$UNIT_GUARD_TIMER"; then
        systemctl --user enable --now "$UNIT_GUARD_TIMER" || { err "启动守护失败: $UNIT_GUARD_TIMER"; return 1; }
    fi
    trap - EXIT TERM INT
    ok "规则代理链路已开启（应用需从应用菜单重启才带代理）"
}

svc_stop() {
    # 先收敛磁盘和 API，后续任何清理失败都不会把核心留回 rule。
    ensure_direct_core || return 1
    local u failed=0
    for u in "$UNIT_GUARD_TIMER" "$UNIT_GUARD_SERVICE" "$UNIT_CONTROLLER"; do
        stop_unit "$u" || { err "关闭失败: $u"; failed=1; }
    done
    python3 "$SCRIPT_DIR/system_proxy.py" stop --state "$KIT_ENV" || { err "系统代理恢复失败"; failed=1; }
    if [ -f "$DESKTOP_BAK/manifest.json" ] && [ -f "$INJECT_PY" ]; then
        python3 "$INJECT_PY" disable --backup-dir "$DESKTOP_BAK" || { err "桌面注入停用失败"; failed=1; }
    fi
    assert_core_owner || { err "direct 核心未保持运行或端口归属异常"; failed=1; }
    [ "$failed" = 0 ] || die "部分代理组件清理失败；核心保持 direct，请查看日志或运行诊断"
    ok "已切换为直连（核心保持运行和自启；系统代理和启动器已恢复）"
}

svc_full_stop() {
    local u failed=0
    for u in "$UNIT_GUARD_TIMER" "$UNIT_GUARD_SERVICE"; do
        stop_unit "$u" || { err "关闭失败: $u"; return 1; }
    done
    # 恢复失败时保留核心，避免产生死代理。
    python3 "$SCRIPT_DIR/system_proxy.py" stop --state "$KIT_ENV" || return 1
    if [ -f "$DESKTOP_BAK/manifest.json" ] && [ -f "$INJECT_PY" ]; then
        python3 "$INJECT_PY" disable --backup-dir "$DESKTOP_BAK" || { err "桌面注入停用失败，核心保留以便恢复"; return 1; }
    fi
    for u in "$UNIT_CONTROLLER" "$UNIT_MIHOMO"; do
        stop_unit "$u" || { err "停止失败: $u"; failed=1; }
    done
    [ "$failed" = 0 ] || die "部分组件关闭失败，请查看日志或运行诊断"
    ok "全部组件已停止并取消自启（系统代理和启动器已恢复）"
}

svc_status() {
    local u
    for u in "$UNIT_MIHOMO" "$UNIT_CONTROLLER" "$UNIT_GUARD_TIMER"; do
        printf '  %-40s %s\n' "$u" "$(systemctl --user is-active "$u" 2>/dev/null || true)"
    done
    printf '  %-40s %s\n' "mihomo.saved-mode" "$(mode_get 2>/dev/null || true)"
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
    start)     svc_start ;;
    stop)      svc_stop ;;
    full-stop) svc_full_stop ;;
    status)    svc_status ;;
    *) die "用法: switch.sh {start|stop|full-stop|status}" ;;
esac

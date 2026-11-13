#!/usr/bin/env bash
# uninstall.sh — openai-proxy-kit 完整回滚
# 逐项交互确认（-y / ASSUME_YES=1 跳过确认），绝不静默删除

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
. "$SCRIPT_DIR/scripts/common.sh"

KIT_ENV="${KIT_ENV:-$HOME/.local/share/openai-proxy-kit/kit.env}"

[ -f "$KIT_ENV" ] || die "未找到安装状态文件: $KIT_ENV（未安装?）"
# shellcheck disable=SC1090
. "$KIT_ENV"

SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

phase "卸载 openai-proxy-kit"
info "安装状态: $KIT_ENV"
echo "  服务: $UNIT_MIHOMO / $UNIT_CONTROLLER / $UNIT_GUARD_TIMER"
echo "  配置: $MIHOMO_CONF_DIR"
echo "  目录: $INSTALL_HOME"

# ---------- 1. systemd 服务 ----------
phase "1/5 停止并移除 systemd 服务"
local_units=("$UNIT_MIHOMO" "$UNIT_CONTROLLER" "$UNIT_GUARD_TIMER" "$UNIT_GUARD_SERVICE")
for u in "${local_units[@]}"; do
    if [ -f "$SYSTEMD_USER_DIR/$u" ]; then
        systemctl --user stop "$u" 2>/dev/null || true
        systemctl --user disable "$u" 2>/dev/null || true
        rm -f "$SYSTEMD_USER_DIR/$u"
        info "已移除 $u"
    fi
done
systemctl --user daemon-reload
ok "systemd 清理完成"

# ---------- 2. 桌面启动器还原 ----------
phase "2/5 还原桌面启动器"
if [ -f "$INSTALL_HOME/backup/desktop/manifest.json" ]; then
    python3 "$SCRIPT_DIR/scripts/inject_desktop.py" restore \
        --backup-dir "$INSTALL_HOME/backup/desktop" \
        --user-apps "$HOME/.local/share/applications" || true
    # 工具自己生成的面板快捷方式
    rm -f "$HOME/.local/share/applications/mihomo-panel.desktop"
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
else
    info "无桌面注入记录，跳过"
fi

# ---------- 3. 系统代理（仅当指向本工具端口时才复位，避免误伤其他部署） ----------
phase "3/5 系统代理复位"
if command -v gsettings >/dev/null 2>&1; then
    MODE=$(gsettings get org.gnome.system.proxy mode 2>/dev/null)
    HTTP_HOST=$(gsettings get org.gnome.system.proxy.http host 2>/dev/null)
    HTTP_PORT=$(gsettings get org.gnome.system.proxy.http port 2>/dev/null)
    if [ "$MODE" = "'manual'" ] && [ "$HTTP_HOST" = "'127.0.0.1'" ] && [ "$HTTP_PORT" = "$MIXED_PORT" ]; then
        if confirm "把 GNOME 系统代理关闭（mode=none）?"; then
            gsettings set org.gnome.system.proxy mode 'none'
            ok "系统代理已关闭"
        else
            warn "保留系统代理 manual（mihomo 停止后流量会不通，请自行处理）"
        fi
    else
        info "系统代理 mode=$MODE host=$HTTP_HOST port=$HTTP_PORT（非本工具端口 $MIXED_PORT，不动）"
    fi
fi

# ---------- 4. 配置目录 ----------
phase "4/5 mihomo 配置目录"
if [ -d "$MIHOMO_CONF_DIR" ]; then
    bak="$INSTALL_HOME/backup/mihomo-conf-$(date +%Y%m%d_%H%M%S).tar.gz"
    mkdir -p "$INSTALL_HOME/backup"
    tar -czf "$bak" -C "$(dirname "$MIHOMO_CONF_DIR")" "$(basename "$MIHOMO_CONF_DIR")" 2>/dev/null || true
    info "已备份: $bak"
    if confirm "删除配置目录 $MIHOMO_CONF_DIR ?"; then
        rm -rf "$MIHOMO_CONF_DIR"
        ok "配置目录已删除"
    else
        warn "保留配置目录（备份仍在）"
    fi
fi

# ---------- 5. 二进制与安装目录 ----------
phase "5/5 二进制与安装目录"
if [ -x "${MIHOMO_BIN:-}" ]; then
    if confirm "删除 mihomo 二进制 $MIHOMO_BIN ?"; then
        rm -f "$MIHOMO_BIN"
        ok "已删除"
    fi
fi
rm -f "$HOME/.local/bin/guard-system-proxy-kit.sh"
if [ -d "$INSTALL_HOME" ]; then
    if confirm "删除安装目录 $INSTALL_HOME（含上游熔断器克隆与备份）?"; then
        rm -rf "$INSTALL_HOME"
        ok "已删除"
    else
        warn "保留 $INSTALL_HOME（含备份，可手动清理）"
    fi
fi

ok "卸载完成"

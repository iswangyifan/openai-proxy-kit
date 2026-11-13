#!/usr/bin/env bash
# install.sh — openai-proxy-kit 总装入口
#
# 用法:
#   ./install.sh                    # 全新安装（交互式）
#   SUB_URL=... ./install.sh       # 免交互指定订阅
#   ./install.sh update            # 重拉订阅重新生成配置并重启 mihomo
#   ./install.sh controller        # 单独安装/补装上游路由熔断器
#   ./install.sh desktop           # 单独注入桌面启动器代理
#   ./install.sh gui               # 单独安装/补装桌面控制台（GTK4）
#   ./install.sh start|stop|status # 一键开关（GUI 按钮背后的同一实现）
#
# 影子验证（不动现网、不碰 gsettings、不写真实应用目录）:
#   INSTALL_HOME=~/.local/share/openai-proxy-kit-test \
#   MIHOMO_CONF_DIR=$HOME/.config/mihomo-kit-shadow \
#   MIXED_PORT=17890 API_PORT=19090 DNS_PORT=1153 PROBE_PORT_BASE=27900 SHADOW=1 ./install.sh
#
# 环境变量可覆盖全部默认值（端口/路径/组名/上游版本等）

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
. "$SCRIPT_DIR/scripts/common.sh"

# ============================================================
# 可配置变量（环境变量优先）
# ============================================================
SUB_URL="${SUB_URL:-}"
MIXED_PORT="${MIXED_PORT:-7890}"
API_PORT="${API_PORT:-9090}"
DNS_PORT="${DNS_PORT:-1053}"
PROBE_PORT_BASE="${PROBE_PORT_BASE:-17900}"
OPENAI_GROUP="${OPENAI_GROUP:-OpenAI 自动选择}"
OPENAI_FILTER="${OPENAI_FILTER:-}"
MAX_NODES="${MAX_NODES:-60}"

INSTALL_HOME="${INSTALL_HOME:-$HOME/.local/share/openai-proxy-kit}"
MIHOMO_CONF_DIR="${MIHOMO_CONF_DIR:-$HOME/.config/openai-proxy-kit/mihomo}"
if [ -x "$SCRIPT_DIR/libexec/mihomo" ]; then
    MIHOMO_BIN="${MIHOMO_BIN:-$SCRIPT_DIR/libexec/mihomo}"
else
    MIHOMO_BIN="${MIHOMO_BIN:-$HOME/.local/bin/mihomo}"
fi
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
USER_APPS_DIR="$HOME/.local/share/applications"
GUARD_BIN="$HOME/.local/bin/guard-system-proxy-kit.sh"
KIT_ENV="${KIT_ENV:-$INSTALL_HOME/kit.env}"

CONTROLLER_BIN="${CONTROLLER_BIN:-$SCRIPT_DIR/libexec/openai-route-controller}"
CONTROLLER_ENV="$INSTALL_HOME/controller.env"
MIHOMO_SECRET="${MIHOMO_SECRET:-}"
MIHOMO_INSTALLED="${MIHOMO_INSTALLED:-0}"

# unit 名固定带 -kit 后缀，避免与用户已有手工部署的裸名 unit 冲突
UNIT_MIHOMO="mihomo-kit.service"
UNIT_CONTROLLER="openai-route-controller-kit.service"
UNIT_GUARD_SERVICE="proxy-guard-kit.service"
UNIT_GUARD_TIMER="proxy-guard-kit.timer"

SHADOW="${SHADOW:-0}"
ACTION="${1:-install}"

# 影子模式强制隔离（绝不覆盖现网配置目录）
if [ "$SHADOW" = "1" ]; then
    MIHOMO_CONF_DIR="${MIHOMO_CONF_DIR_OVERRIDE:-$HOME/.config/mihomo-kit-shadow}"
    UNIT_MIHOMO="mihomo-kit-test.service"
    UNIT_CONTROLLER="openai-route-controller-kit-test.service"
    UNIT_GUARD_SERVICE="proxy-guard-kit-test.service"
    UNIT_GUARD_TIMER="proxy-guard-kit-test.timer"
    if [ "$PROBE_PORT_BASE" != "17900" ]; then
        warn "PROBE_PORT_BASE=$PROBE_PORT_BASE 与上游 controller 硬编码 17900 不一致："
        warn "  影子冒烟中 controller 的 probe 探测将不可用（仅验证 API 读取/组识别）"
    fi
fi

write_kit_env() {
    export STATE_VERSION=2
    GUARD_LOG="${GUARD_LOG:-$INSTALL_HOME/proxy-guard.log}"
    export INSTALL_HOME MIXED_PORT API_PORT DNS_PORT PROBE_PORT_BASE OPENAI_GROUP OPENAI_FILTER MAX_NODES
    export MIHOMO_CONF_DIR MIHOMO_BIN MIHOMO_SECRET SUB_URL SHADOW GUARD_LOG UNIT_MIHOMO UNIT_CONTROLLER
    export UNIT_GUARD_SERVICE UNIT_GUARD_TIMER MIHOMO_INSTALLED CONTROLLER_BIN CONTROLLER_ENV
    python3 "$SCRIPT_DIR/scripts/kit_state.py" write "$KIT_ENV"
}

# ============================================================
# 阶段 1: 环境检测
# ============================================================
check_env() {
    phase "阶段 1/9 环境检测"

    [ "$(uname -s)" = "Linux" ] || die "本工具仅支持 Linux"
    systemctl --user is-system-running >/dev/null 2>&1 || true
    systemctl --user show-environment >/dev/null 2>&1 || die "systemd --user 不可用（无图形会话?）"

    if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes; then
        warn "用户 linger 未开启（注销后服务会停）: loginctl enable-linger $USER"
    fi

    IS_GNOME=0
    if is_gnome; then IS_GNOME=1; else warn "未检测到 GNOME：proxy-guard 与系统代理段将跳过"; fi

    # Clash 系软件冲突检测（实战: FlClash 抢 7890 且退出时关系统代理）
    if pgrep -if 'flclash' >/dev/null 2>&1; then
        die "检测到 FlClash 正在运行。请先完全退出 FlClash 再安装（端口冲突 + 退出时它会关闭系统代理开关）"
    fi
    if pgrep -if 'clash-verge|verge-mihomo' >/dev/null 2>&1; then
        die "检测到 Clash Verge 正在运行。请先完全退出再安装（端口冲突）"
    fi

    if [ "$SHADOW" != 1 ] && [ "$PROBE_PORT_BASE" != 17900 ]; then
        die "当前 controller 的探测端口固定从 17900 开始；普通安装请保留默认值"
    fi
    # 端口占用检测
    local p
    for p in "$MIXED_PORT" "$API_PORT" "$DNS_PORT"; do
        if port_in_use "$p"; then
            die "端口 $p 已被占用（$(
                port_user "$p" || echo unknown
            )）。请退出占用程序或用 MIXED_PORT/API_PORT/DNS_PORT 环境变量换端口"
        fi
    done

    command -v python3 >/dev/null 2>&1 || die "缺少 python3"
    if ! have_pyyaml; then
        warn "缺少 python3-yaml。安装: sudo apt install python3-yaml"
        if confirm "现在安装 python3-yaml? [需要 sudo]"; then
            sudo apt install -y python3-yaml || die "python3-yaml 安装失败"
        else
            die "python3-yaml 是配置生成器的必需依赖"
        fi
    fi
    ok "环境检测通过"
}

# ============================================================
# 阶段 2: mihomo 安装
# ============================================================
install_mihomo() {
    phase "阶段 2/9 mihomo 核心"
    if [ -x "$MIHOMO_BIN" ]; then
        ok "已存在 $($MIHOMO_BIN -v 2>/dev/null | head -1 | cut -d' ' -f1-3)，复用: $MIHOMO_BIN"
        MIHOMO_INSTALLED=0   # 复用已有二进制（可能是用户其他部署的），卸载时不删
        return
    fi
    bash "$SCRIPT_DIR/packaging/fetch-core.sh" || die "固定核心下载或校验失败"
    mkdir -p "$(dirname "$MIHOMO_BIN")"
    install -m 0755 "$SCRIPT_DIR/.build-cache/core/mihomo" "$MIHOMO_BIN"
    MIHOMO_INSTALLED=1
    ok "已安装通过固定摘要验证的 mihomo"

}

# ============================================================
# 阶段 4: 订阅配置生成
# ============================================================
gen_mihomo_config() {
    phase "阶段 4/9 订阅配置生成"
    if [ -z "$SUB_URL" ]; then
        [ "$ASSUME_YES" = "1" ] && die "免交互模式必须用 SUB_URL=... 指定订阅"
        read -r -p "请输入机场订阅 URL: " SUB_URL </dev/tty
        [ -n "$SUB_URL" ] || die "订阅 URL 为空"
    fi
    mkdir -p "$MIHOMO_CONF_DIR"
    local extra=()
    [ -n "$OPENAI_FILTER" ] && extra+=(--openai-filter "$OPENAI_FILTER")

    local out_tmp="$MIHOMO_CONF_DIR/config.yaml.new"
    SUB_URL="$SUB_URL" MIHOMO_SECRET="$MIHOMO_SECRET" python3 "$SCRIPT_DIR/scripts/gen_config.py" \
        --out "$out_tmp" \
        --mixed-port "$MIXED_PORT" --api-port "$API_PORT" --dns-port "$DNS_PORT" \
        --probe-port-base "$PROBE_PORT_BASE" --openai-group "$OPENAI_GROUP" \
        --max-nodes "$MAX_NODES" --mihomo-bin "$MIHOMO_BIN" \
        --mihomo-conf-dir "$MIHOMO_CONF_DIR" "${extra[@]}" \
        || die "配置生成失败"

    # 原子替换 + 备份旧配置
    if [ -f "$MIHOMO_CONF_DIR/config.yaml" ]; then
        cp "$MIHOMO_CONF_DIR/config.yaml" "$MIHOMO_CONF_DIR/config.yaml.kit-bak"
        info "旧配置已备份为 config.yaml.kit-bak"
    fi
    mv "$out_tmp" "$MIHOMO_CONF_DIR/config.yaml"
    chmod 600 "$MIHOMO_CONF_DIR/config.yaml"
    ok "配置已生成: $MIHOMO_CONF_DIR/config.yaml"
}

# ============================================================
# 阶段 3: GeoIP 数据（须先于配置生成——mihomo -t 校验 GEOSITE 规则时需要）
# ============================================================
install_geodata() {
    mkdir -p "$MIHOMO_CONF_DIR"
    info "仅 OpenAI 域名分流，无需下载公共数据库或第三方网页面板"
}

# ============================================================
# 阶段 5: systemd 服务
# ============================================================
install_systemd_units() {
    phase "阶段 5/9 systemd 服务"
    mkdir -p "$SYSTEMD_USER_DIR"
    render_template "$SCRIPT_DIR/templates/mihomo.service.tmpl" \
        "$SYSTEMD_USER_DIR/$UNIT_MIHOMO" \
        MIHOMO_BIN="$MIHOMO_BIN" MIHOMO_CONF_DIR="$MIHOMO_CONF_DIR" \
        PROXY_HELPER="$SCRIPT_DIR/scripts/system_proxy.py" KIT_ENV="$KIT_ENV"
    systemctl --user daemon-reload
    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze verify "$SYSTEMD_USER_DIR/$UNIT_MIHOMO" 2>&1 | head -5 || true
    fi
    ok "$UNIT_MIHOMO 已配置（统一启停入口负责启动）"
}

# ============================================================
# 阶段 6: proxy-guard（系统代理守护，仅 GNOME 非 shadow）
# ============================================================
install_proxy_guard() {
    phase "阶段 6/9 proxy-guard 系统代理守护"
    if [ "$SHADOW" = "1" ]; then
        info "影子模式: 跳过（避免写真实 gsettings）"
        return
    fi
    if [ "${IS_GNOME:-0}" != "1" ]; then
        info "非 GNOME 环境: 跳过"
        return
    fi
    mkdir -p "$(dirname "$GUARD_BIN")"
    render_template "$SCRIPT_DIR/templates/guard-system-proxy.sh.tmpl" "$GUARD_BIN" KIT_ENV="$KIT_ENV" PROXY_HELPER="$SCRIPT_DIR/scripts/system_proxy.py"
    chmod +x "$GUARD_BIN"
    render_template "$SCRIPT_DIR/templates/proxy-guard.service.tmpl" \
        "$SYSTEMD_USER_DIR/$UNIT_GUARD_SERVICE" \
        GUARD_SCRIPT="$GUARD_BIN" KIT_ENV="$KIT_ENV" PROXY_HELPER="$SCRIPT_DIR/scripts/system_proxy.py"
    render_template "$SCRIPT_DIR/templates/proxy-guard.timer.tmpl" \
        "$SYSTEMD_USER_DIR/$UNIT_GUARD_TIMER" UNIT_GUARD_SERVICE="$UNIT_GUARD_SERVICE"
    systemctl --user daemon-reload
    ok "proxy-guard 已配置（只清理失效代理，不接管其他代理设置）"
}

# ============================================================
# 阶段 7: 上游路由熔断器
# ============================================================
install_controller() {
    phase "阶段 7/9 内置路由熔断器"
    local was_active=0
    systemctl --user is-active --quiet "$UNIT_CONTROLLER" && was_active=1 || true
    if [ ! -x "$CONTROLLER_BIN" ]; then
        warn "未找到内置 controller 二进制。建议安装独立 .deb 软件包。"
        warn "源码开发可先运行 packaging/build-linux.sh 构建；不再克隆上游仓库或安装 Node 运行时。"
        return 1
    fi
    mkdir -p "$INSTALL_HOME/controller" "$SYSTEMD_USER_DIR"
    python3 "$SCRIPT_DIR/scripts/kit_state.py" controller "$CONTROLLER_ENV"


    render_template "$SCRIPT_DIR/templates/openai-route-controller.service.tmpl" \
        "$SYSTEMD_USER_DIR/$UNIT_CONTROLLER" \
        CONTROLLER_BIN="$CONTROLLER_BIN" CONTROLLER_ENV="$CONTROLLER_ENV" UNIT_MIHOMO="$UNIT_MIHOMO"
    systemctl --user daemon-reload

    if [ "$SHADOW" = "1" ]; then
        ok "影子模式: $UNIT_CONTROLLER 已渲染（不启动）"
    elif systemctl --user is-active --quiet "$UNIT_MIHOMO"; then
        systemctl --user enable "$UNIT_CONTROLLER"
        if [ "$was_active" = 1 ]; then
            systemctl --user restart "$UNIT_CONTROLLER"
        else
            systemctl --user start "$UNIT_CONTROLLER"
        fi
        sleep 2
        systemctl --user is-active --quiet "$UNIT_CONTROLLER" \
            || die "$UNIT_CONTROLLER 启动失败，查看: journalctl --user -u $UNIT_CONTROLLER -n 30"
        ok "$UNIT_CONTROLLER 已启动"
    else
        if [ "$was_active" = 1 ]; then systemctl --user stop "$UNIT_CONTROLLER"; fi
        ok "内置 controller 已配置；核心未开启，保持停止状态"
    fi
    return 0
}

# ============================================================
# 阶段 8: 桌面启动器代理注入
# ============================================================
install_desktop_inject() {
    phase "阶段 8/9 桌面启动器代理注入"
    if [ "$SHADOW" = "1" ]; then
        info "影子模式: 跳过（避免写真实应用目录）"
        return
    fi
    [ "${DESKTOP_INJECT:-0}" = 1 ] || { info "未授权启动器适配，跳过；可单独运行 desktop 子命令"; return; }
    info "背景: VSCode 的 Codex 插件、ChatGPT 桌面版等不读系统代理、只认环境变量"
    local candidates=()
    local sys_apps="/usr/share/applications"
    local f
    for f in "$sys_apps"/*.desktop; do
        [ -f "$f" ] || continue
        case "$(basename "$f")" in
            *code*|*Code*|*chatgpt*|*ChatGPT*|*cursor*|*Cursor*) candidates+=("$f") ;;
        esac
    done
    [ ${#candidates[@]} -gt 0 ] || { info "未发现候选应用（可稍后运行: ./install.sh desktop）"; return; }

    local chosen=()
    for f in "${candidates[@]}"; do
        if confirm "为 $(basename "$f") 注入代理环境变量?"; then
            chosen+=("$f")
        fi
    done
    if [ "${#chosen[@]}" -eq 0 ]; then info "未选择任何应用"; return; fi

    local args=()
    for f in "${chosen[@]}"; do args+=(--desktop-file "$f"); done
    python3 "$SCRIPT_DIR/scripts/inject_desktop.py" inject \
        --proxy "http://127.0.0.1:$MIXED_PORT" \
        --backup-dir "$INSTALL_HOME/backup/desktop" \
        "${args[@]}"
    ok "注入完成（应用需完全退出后从应用菜单重启生效）"
}

# ============================================================
# 阶段 9: 桌面控制台（GTK4，FlClash 风格一键开关窗口）
# ============================================================
GUI_INSTALLED=0
install_gui() {
    phase "阶段 9/10 桌面控制台（GTK4）"
    if [ "$SHADOW" = "1" ]; then
        info "影子模式: 跳过（不写真实应用目录）"
        return
    fi
    if [ -f "$SCRIPT_DIR/packaged" ]; then
        GUI_INSTALLED=1
        ok "独立软件包已提供桌面控制台与托盘入口"
        return
    fi
    if ! python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null; then
        warn "缺少 GTK4/libadwaita（python3-gi），跳过 GUI。补装: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adwaita-1"
        warn "之后运行 ./install.sh gui 补装。CLI 一键开关不受影响: ./install.sh start|stop|status"
        return
    fi
    mkdir -p "$USER_APPS_DIR"
    mkdir -p "$INSTALL_HOME/scripts" "$INSTALL_HOME/templates"
    if [ "$SCRIPT_DIR" != "$INSTALL_HOME" ]; then
        cp "$SCRIPT_DIR/scripts/"*.py "$SCRIPT_DIR/scripts/"*.sh "$INSTALL_HOME/scripts/"
        cp "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/uninstall.sh" "$INSTALL_HOME/"
        cp "$SCRIPT_DIR/templates/"* "$INSTALL_HOME/templates/"
    fi
    mkdir -p "$INSTALL_HOME/assets" "$INSTALL_HOME/templates"
    cp -R "$SCRIPT_DIR/assets/." "$INSTALL_HOME/assets/"
    cp "$SCRIPT_DIR/diagnose.sh" "$INSTALL_HOME/diagnose.sh"
    render_template "$SCRIPT_DIR/templates/openai-proxy-kit-console.desktop.tmpl" \
        "$USER_APPS_DIR/openai-proxy-kit-console.desktop" \
        INSTALL_HOME="$INSTALL_HOME"
    update-desktop-database "$USER_APPS_DIR" 2>/dev/null || true
    GUI_INSTALLED=1
    ok "应用菜单已添加「代理控制台」"
}

# ============================================================
# 阶段 10: 冒烟验证
# ============================================================
smoke_test() {
    phase "阶段 10/10 冒烟验证"
    local shadow_pid=""
    if [ "$SHADOW" = "1" ]; then
        info "影子模式: 手动拉起 mihomo 做冒烟（结束即杀）"
        "$MIHOMO_BIN" -d "$MIHOMO_CONF_DIR" >/dev/null 2>&1 &
        shadow_pid=$!
        trap 'kill "$shadow_pid" 2>/dev/null || true; wait "$shadow_pid" 2>/dev/null || true' EXIT
        sleep 2
        kill -0 "$shadow_pid" 2>/dev/null || die "影子 mihomo 启动即退出（配置问题?）"
    else
        systemctl --user is-active --quiet "$UNIT_MIHOMO" || die "mihomo 服务未运行"
    fi

    local fail=0
    # 1. 代理端口监听
    if port_in_use "$MIXED_PORT"; then ok "代理端口 $MIXED_PORT 监听中"; else err "代理端口 $MIXED_PORT 未监听"; fail=1; fi
    # 2. API 端口
    if port_in_use "$API_PORT"; then ok "API 端口 $API_PORT 监听中"; else err "API 端口 $API_PORT 未监听"; fail=1; fi
    # 3. OpenAI 组存在且候选非空
    local group_json
    group_json=$(api_curl -fsS --noproxy '*' --max-time 5 \
        "http://127.0.0.1:$API_PORT/proxies/$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$OPENAI_GROUP")") || group_json=""
    if echo "$group_json" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('all')" 2>/dev/null; then
        ok "OpenAI 组就绪: 当前选中 $(echo "$group_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['now'])")"
    else
        err "OpenAI 组查询失败"; fail=1
    fi
    # 4. 经代理访问 chatgpt.com
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
        -x "http://127.0.0.1:$MIXED_PORT" https://chatgpt.com) || code="000"
    case "$code" in
        200|301|302|307|308) ok "经代理访问 chatgpt.com: HTTP $code" ;;
        *) warn "经代理访问 chatgpt.com: HTTP $code（节点质量问题不影响工具部署，可稍后换节点）" ;;
    esac
    # 5. probe 端口抽查（首个候选）
    if port_in_use "$PROBE_PORT_BASE"; then
        ok "probe 端口 $PROBE_PORT_BASE 监听中（共 $(ss -tlnH "sport >= :$PROBE_PORT_BASE and sport <= :$((PROBE_PORT_BASE + MAX_NODES))" 2>/dev/null | wc -l) 个）"
    else
        warn "probe 端口 $PROBE_PORT_BASE 未监听（listeners 未生效?）"
    fi
    # 6. controller 影子联调（部分验证: API 读取 + 组识别，probe 受端口约束可能不可用）
    if [ "$SHADOW" = "1" ] && [ -x "$CONTROLLER_BIN" ]; then
        info "controller 影子联调 (--once --shadow，只读不改选择)..."
        local shadow_state
        shadow_state=$(mktemp -d)
        if MIHOMO_API="http://127.0.0.1:$API_PORT" \
           MIHOMO_PROXY="http://127.0.0.1:$MIXED_PORT" \
           OPENAI_GROUP="$OPENAI_GROUP" \
           STATE_PATH="$shadow_state/state.json" \
           timeout 60 "$CONTROLLER_BIN" --once --shadow 2>&1 | tail -3; then
            ok "controller 影子联调完成（详见上方输出）"
        else
            warn "controller 影子联调异常（详见上方输出）"
        fi
        rm -rf "$shadow_state"
    fi

    if [ -n "$shadow_pid" ]; then
        kill "$shadow_pid" 2>/dev/null || true
        wait "$shadow_pid" 2>/dev/null || true
        trap - EXIT
        wait "$shadow_pid" 2>/dev/null || true
        info "影子 mihomo 已停止"
    fi

    echo
    if [ "$fail" = "0" ]; then
        ok "冒烟验证完成"
    else
        err "冒烟验证存在失败项，排查: ./diagnose.sh"
        exit 1
    fi
}

print_summary() {
    phase "安装汇总"
    cat <<EOF
  安装目录     $INSTALL_HOME
  mihomo 配置  $MIHOMO_CONF_DIR/config.yaml
  代理端口     mixed=$MIXED_PORT  API=$API_PORT  DNS=$DNS_PORT  probe=$PROBE_PORT_BASE+
  OpenAI 组    $OPENAI_GROUP
  服务         $UNIT_MIHOMO $UNIT_CONTROLLER $UNIT_GUARD_TIMER
  管理面板     http://127.0.0.1:$API_PORT/ui/
  状态文件     $KIT_ENV
  一键开关     ./install.sh start|stop|status
$( [ "$GUI_INSTALLED" = "1" ] && echo "  桌面控制台   应用菜单「代理控制台」（FlClash 风格 GTK4 窗口）" )
$( [ "$SHADOW" = "1" ] && echo "  ⚠ 影子模式: systemd unit 未启动，系统代理/桌面注入/GUI 未执行" )
  诊断工具     ./diagnose.sh
  更新订阅     ./install.sh update
  卸载         ./uninstall.sh
EOF
}

# ============================================================
# 主流程
# ============================================================
main() {
    case "$ACTION" in
        install)
            [ -f "$KIT_ENV" ] && warn "检测到已有安装状态（$KIT_ENV），将按当前变量覆盖式重装"
            lock_kit
            MIHOMO_SECRET="${MIHOMO_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
            check_env
            install_mihomo
            install_geodata
            gen_mihomo_config
            write_kit_env
            trap 'code=$?; trap - EXIT; if [ "$code" != 0 ] && [ "$SHADOW" != 1 ]; then bash "$SCRIPT_DIR/scripts/switch.sh" stop || true; fi' EXIT
            trap 'exit 143' TERM
            trap 'exit 130' INT
            install_systemd_units
            install_proxy_guard
            install_controller || true
            write_kit_env
            install_desktop_inject
            install_gui
            if [ "$SHADOW" != 1 ]; then
                bash "$SCRIPT_DIR/scripts/switch.sh" start
            fi
            smoke_test
            print_summary
            trap - EXIT TERM INT
            ;;
        update)
            [ -f "$KIT_ENV" ] || die "未安装（找不到 $KIT_ENV）"
            # shellcheck disable=SC1090
            lock_kit
            load_kit_env || die "安装状态无效，请重新配置"
            [ -n "$SUB_URL" ] || die "kit.env 缺少 SUB_URL"
            local was_running=0
            systemctl --user is-active --quiet "$UNIT_MIHOMO" && was_running=1 || true
            [ -n "$MIHOMO_SECRET" ] || MIHOMO_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
            gen_mihomo_config
            write_kit_env
            install_controller
            if [ "$SHADOW" != "1" ] && [ "$was_running" = 1 ]; then
                if ! systemctl --user restart "$UNIT_MIHOMO"; then
                    cp "$MIHOMO_CONF_DIR/config.yaml.kit-bak" "$MIHOMO_CONF_DIR/config.yaml"
                    systemctl --user restart "$UNIT_MIHOMO" || true
                    die "更新失败，已恢复旧配置"
                fi
            fi
            ok "订阅更新完成"
            ;;
        controller)
            [ -f "$KIT_ENV" ] || die "未安装（先跑完整 install）"
            # shellcheck disable=SC1090
            lock_kit
            load_kit_env || die "安装状态无效，请重新配置"
            if [ -x "$SCRIPT_DIR/libexec/openai-route-controller" ]; then
                CONTROLLER_BIN="$SCRIPT_DIR/libexec/openai-route-controller"
            fi
            if [ -x "$SCRIPT_DIR/libexec/mihomo" ]; then
                MIHOMO_BIN="$SCRIPT_DIR/libexec/mihomo"
                MIHOMO_INSTALLED=0
            fi
            local was_running=0 migrating=0
            systemctl --user is-active --quiet "$UNIT_MIHOMO" && was_running=1 || true
            if [ -z "$MIHOMO_SECRET" ]; then
                migrating=1
                MIHOMO_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
                export MIHOMO_SECRET
                python3 "$SCRIPT_DIR/scripts/secure_config.py" "$MIHOMO_CONF_DIR/config.yaml"
            fi
            write_kit_env
            IS_GNOME=0; is_gnome && IS_GNOME=1 || true
            if [ "$IS_GNOME" = 1 ]; then python3 "$SCRIPT_DIR/scripts/system_proxy.py" adopt --state "$KIT_ENV"; fi
            install_systemd_units
            install_proxy_guard
            install_controller || die "controller 安装失败"
            if [ "$was_running" = 1 ]; then
                systemctl --user restart "$UNIT_MIHOMO"
            fi

            ;;
        desktop)
            [ -f "$KIT_ENV" ] || die "未安装（先跑完整 install）"
            # shellcheck disable=SC1090
            lock_kit
            load_kit_env || die "安装状态无效，请重新配置"
            DESKTOP_INJECT=1 IS_GNOME=1 SHADOW=0 install_desktop_inject
            ;;
        gui)
            [ -f "$KIT_ENV" ] || die "未安装（先跑完整 install）"
            # shellcheck disable=SC1090
            lock_kit
            load_kit_env || die "安装状态无效，请重新配置"
            SHADOW=0 install_gui
            ;;
        start|stop|status)
            exec bash "$SCRIPT_DIR/scripts/switch.sh" "$ACTION"
            ;;
        logs)
            shift
            exec /usr/bin/python3 "$SCRIPT_DIR/scripts/runtime.py" "$@"
            ;;
        *)
            die "未知子命令: $ACTION（可用: install/update/controller/desktop/gui/start/stop/status）"
            ;;
    esac
}

main

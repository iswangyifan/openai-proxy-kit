#!/usr/bin/env bash
# install.sh — openai-proxy-kit 总装入口
#
# 用法:
#   ./install.sh                    # 全新安装（交互式）
#   SUB_URL=... ./install.sh       # 免交互指定订阅
#   ./install.sh update            # 重拉订阅重新生成配置并重启 mihomo
#   ./install.sh controller        # 单独安装/补装上游路由熔断器
#   ./install.sh desktop           # 单独注入桌面启动器代理
#
# 影子验证（不动现网、不碰 gsettings、不写真实应用目录）:
#   INSTALL_HOME=~/.local/share/openai-proxy-kit-test \
#   MIHOMO_CONF_DIR=$HOME/.config/mihomo-kit-shadow \
#   MIXED_PORT=17890 API_PORT=19090 DNS_PORT=1153 SHADOW=1 ./install.sh
#
# 环境变量可覆盖全部默认值（端口/路径/组名/上游版本等）

set -euo pipefail

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
MIHOMO_CONF_DIR="${MIHOMO_CONF_DIR:-$HOME/.config/mihomo}"
MIHOMO_BIN="${MIHOMO_BIN:-$HOME/.local/bin/mihomo}"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
USER_APPS_DIR="$HOME/.local/share/applications"
GUARD_BIN="$HOME/.local/bin/guard-system-proxy-kit.sh"
KIT_ENV="${KIT_ENV:-$INSTALL_HOME/kit.env}"

UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/tianwdong/openai-route-controller}"
UPSTREAM_REF="${UPSTREAM_REF:-main}"
CONTROLLER_DIR="$INSTALL_HOME/openai-route-controller"
CONTROLLER_ENV="$INSTALL_HOME/controller.env"

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
    mkdir -p "$INSTALL_HOME"
    # 注意: 值统一用单引号包裹（OPENAI_GROUP 等含空格/中文，裸值会导致 source 时被当命令执行）
    cat > "$KIT_ENV" <<EOF
# openai-proxy-kit 安装状态（install.sh 生成，diagnose/uninstall/update 读取）
INSTALL_HOME='$INSTALL_HOME'
MIXED_PORT='$MIXED_PORT'
API_PORT='$API_PORT'
DNS_PORT='$DNS_PORT'
PROBE_PORT_BASE='$PROBE_PORT_BASE'
OPENAI_GROUP='$OPENAI_GROUP'
MIHOMO_CONF_DIR='$MIHOMO_CONF_DIR'
MIHOMO_BIN='$MIHOMO_BIN'
SUB_URL='$SUB_URL'
SHADOW='$SHADOW'
GUARD_LOG='$INSTALL_HOME/proxy-guard.log'
UNIT_MIHOMO='$UNIT_MIHOMO'
UNIT_CONTROLLER='$UNIT_CONTROLLER'
UNIT_GUARD_SERVICE='$UNIT_GUARD_SERVICE'
UNIT_GUARD_TIMER='$UNIT_GUARD_TIMER'
NODE_BIN='${NODE_BIN:-}'
CONTROLLER_DIR='$CONTROLLER_DIR'
CONTROLLER_ENV='$CONTROLLER_ENV'
EOF
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
        return
    fi
    local arch tag asset tmpdir
    case "$(uname -m)" in
        x86_64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) die "不支持的架构: $(uname -m)" ;;
    esac
    info "获取 mihomo 最新版本号..."
    tag=$(curl -fsSL --connect-timeout 10 --max-time 30 -o /dev/null -w '%{url_effective}' \
        https://github.com/MetaCubeX/mihomo/releases/latest | grep -oE 'v[0-9.]+') || tag=""
    [ -n "$tag" ] || tag="v1.19.33"
    asset="mihomo-linux-${arch}-${tag}.gz"
    tmpdir=$(mktemp -d)
    info "下载 $asset ..."
    dl "https://github.com/MetaCubeX/mihomo/releases/download/$tag/$asset" "$tmpdir/mihomo.gz" \
        || die "mihomo 下载失败（可手动下载解压到 $MIHOMO_BIN 后重跑）"
    gunzip -f "$tmpdir/mihomo.gz"
    mkdir -p "$(dirname "$MIHOMO_BIN")"
    install -m 0755 "$tmpdir/mihomo" "$MIHOMO_BIN"
    rm -rf "$tmpdir"
    ok "已安装 $($MIHOMO_BIN -v | head -1 | cut -d' ' -f1-3)"
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
    python3 "$SCRIPT_DIR/scripts/gen_config.py" \
        --sub-url "$SUB_URL" \
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
    ok "配置已生成: $MIHOMO_CONF_DIR/config.yaml"
}

# ============================================================
# 阶段 3: GeoIP 数据（须先于配置生成——mihomo -t 校验 GEOSITE 规则时需要）
# ============================================================
install_geodata() {
    phase "阶段 3/9 GeoIP/GeoSite 数据"
    local base="https://github.com/MetaCubeX/meta-rules-dat/releases/latest/download"
    local f
    for f in country.mmdb geosite.dat geoip.metadb; do
        if [ -f "$MIHOMO_CONF_DIR/$f" ]; then
            info "$f 已存在，跳过"
            continue
        fi
        info "下载 $f ..."
        if dl "$base/$f" "$MIHOMO_CONF_DIR/$f"; then
            ok "$f 已就位"
        else
            rm -f "$MIHOMO_CONF_DIR/$f"
            warn "$f 下载失败：mihomo 启动时会自行下载（国内网络可能卡住）。"
            warn "  可手动从其他机器拷贝到 $MIHOMO_CONF_DIR/ 后重启服务"
        fi
    done
    # metacubexd 管理面板（external-ui）
    if [ ! -d "$MIHOMO_CONF_DIR/ui" ] || [ -z "$(ls -A "$MIHOMO_CONF_DIR/ui" 2>/dev/null)" ]; then
        local tmpdir=$(mktemp -d)
        info "下载 metacubexd 管理面板..."
        if dl "https://github.com/MetaCubeX/metacubexd/releases/latest/download/compressed-dist.tgz" \
            "$tmpdir/dist.tgz"; then
            mkdir -p "$MIHOMO_CONF_DIR/ui"
            tar -xzf "$tmpdir/dist.tgz" -C "$MIHOMO_CONF_DIR/ui"
            ok "管理面板已就位（http://127.0.0.1:$API_PORT/ui/）"
        else
            warn "metacubexd 下载失败（不影响核心功能，仅管理面板不可用）"
            mkdir -p "$MIHOMO_CONF_DIR/ui"
        fi
        rm -rf "$tmpdir"
    fi
}

# ============================================================
# 阶段 5: systemd 服务
# ============================================================
install_systemd_units() {
    phase "阶段 5/9 systemd 服务"
    mkdir -p "$SYSTEMD_USER_DIR"
    render_template "$SCRIPT_DIR/templates/mihomo.service.tmpl" \
        "$SYSTEMD_USER_DIR/$UNIT_MIHOMO" \
        MIHOMO_BIN="$MIHOMO_BIN" MIHOMO_CONF_DIR="$MIHOMO_CONF_DIR"
    systemctl --user daemon-reload
    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze verify "$SYSTEMD_USER_DIR/$UNIT_MIHOMO" 2>&1 | head -5 || true
    fi
    if [ "$SHADOW" = "1" ]; then
        ok "影子模式: $UNIT_MIHOMO 已渲染（不启动、不 enable）"
    else
        systemctl --user enable --now "$UNIT_MIHOMO"
        sleep 1
        systemctl --user is-active --quiet "$UNIT_MIHOMO" \
            || die "$UNIT_MIHOMO 启动失败，查看: journalctl --user -u $UNIT_MIHOMO -n 30"
        ok "$UNIT_MIHOMO 已启动"
    fi
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
    render_template "$SCRIPT_DIR/templates/guard-system-proxy.sh.tmpl" "$GUARD_BIN" KIT_ENV="$KIT_ENV"
    chmod +x "$GUARD_BIN"
    render_template "$SCRIPT_DIR/templates/proxy-guard.service.tmpl" \
        "$SYSTEMD_USER_DIR/$UNIT_GUARD_SERVICE" \
        GUARD_SCRIPT="$GUARD_BIN" KIT_ENV="$KIT_ENV"
    render_template "$SCRIPT_DIR/templates/proxy-guard.timer.tmpl" \
        "$SYSTEMD_USER_DIR/$UNIT_GUARD_TIMER"
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT_GUARD_TIMER"
    # 立即执行一次，马上把系统代理指到 mihomo
    systemctl --user start "$UNIT_GUARD_SERVICE"
    ok "proxy-guard 已启用（每分钟守护系统代理开关）"
}

# ============================================================
# 阶段 7: 上游路由熔断器
# ============================================================
install_controller() {
    phase "阶段 7/9 openai-route-controller 路由熔断器"
    local node_bin
    node_bin=$(command -v node 2>/dev/null || true)
    if [ -z "$node_bin" ] || [ "$(node_major)" -lt 22 ]; then
        warn "需要 Node.js >= 22（当前: $(node -v 2>/dev/null || echo 未安装)）"
        warn "跳过熔断器安装。装好 Node 后运行: ./install.sh controller"
        return 1
    fi

    if [ ! -d "$CONTROLLER_DIR/.git" ]; then
        mkdir -p "$INSTALL_HOME"
        info "克隆上游 $UPSTREAM_REPO ($UPSTREAM_REF)..."
        git clone --depth 1 -b "$UPSTREAM_REF" "$UPSTREAM_REPO" "$CONTROLLER_DIR" \
            || die "上游克隆失败（网络?）。可手动 clone 到 $CONTROLLER_DIR 后重跑 ./install.sh controller"
    else
        info "上游已存在: $CONTROLLER_DIR"
    fi

    NODE_BIN="$node_bin"  # 记入 kit.env
    render_template "$SCRIPT_DIR/templates/controller.env.tmpl" "$CONTROLLER_ENV" \
        API_PORT="$API_PORT" MIXED_PORT="$MIXED_PORT" OPENAI_GROUP="$OPENAI_GROUP" \
        INSTALL_HOME="$INSTALL_HOME"
    mkdir -p "$INSTALL_HOME/controller"

    render_template "$SCRIPT_DIR/templates/openai-route-controller.service.tmpl" \
        "$SYSTEMD_USER_DIR/$UNIT_CONTROLLER" \
        CONTROLLER_DIR="$CONTROLLER_DIR" CONTROLLER_ENV="$CONTROLLER_ENV" NODE_BIN="$node_bin"
    systemctl --user daemon-reload

    if [ "$SHADOW" = "1" ]; then
        ok "影子模式: $UNIT_CONTROLLER 已渲染（不启动）"
    else
        systemctl --user enable --now "$UNIT_CONTROLLER"
        sleep 2
        systemctl --user is-active --quiet "$UNIT_CONTROLLER" \
            || die "$UNIT_CONTROLLER 启动失败，查看: journalctl --user -u $UNIT_CONTROLLER -n 30"
        ok "$UNIT_CONTROLLER 已启动"
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
# 阶段 9: 冒烟验证
# ============================================================
smoke_test() {
    phase "阶段 9/9 冒烟验证"
    local shadow_pid=""
    if [ "$SHADOW" = "1" ]; then
        info "影子模式: 手动拉起 mihomo 做冒烟（结束即杀）"
        "$MIHOMO_BIN" -d "$MIHOMO_CONF_DIR" >/dev/null 2>&1 &
        shadow_pid=$!
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
    group_json=$(curl -s --max-time 5 \
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
    if [ "$SHADOW" = "1" ] && [ -f "$CONTROLLER_DIR/controller.mjs" ] && [ "$(node_major)" -ge 22 ]; then
        info "controller 影子联调 (--once --shadow，只读不改选择)..."
        local shadow_state
        shadow_state=$(mktemp -d)
        if MIHOMO_API="http://127.0.0.1:$API_PORT" \
           MIHOMO_PROXY="http://127.0.0.1:$MIXED_PORT" \
           OPENAI_GROUP="$OPENAI_GROUP" \
           STATE_PATH="$shadow_state/state.json" \
           timeout 60 node "$CONTROLLER_DIR/controller.mjs" --once --shadow 2>&1 | tail -3; then
            ok "controller 影子联调完成（详见上方输出）"
        else
            warn "controller 影子联调异常（详见上方输出）"
        fi
        rm -rf "$shadow_state"
    fi

    if [ -n "$shadow_pid" ]; then
        kill "$shadow_pid" 2>/dev/null || true
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
$( [ "$SHADOW" = "1" ] && echo "  ⚠ 影子模式: systemd unit 未启动，系统代理/桌面注入未执行" )
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
            check_env
            install_mihomo
            install_geodata
            gen_mihomo_config
            install_systemd_units
            install_proxy_guard
            write_kit_env   # controller 需要 kit.env 先落盘（NODE_BIN 后补写）
            install_controller || true
            write_kit_env   # 覆盖写入（含 NODE_BIN）
            install_desktop_inject
            smoke_test
            print_summary
            ;;
        update)
            [ -f "$KIT_ENV" ] || die "未安装（找不到 $KIT_ENV）"
            # shellcheck disable=SC1090
            . "$KIT_ENV"
            [ -n "$SUB_URL" ] || die "kit.env 缺少 SUB_URL"
            info "重拉订阅重新生成配置..."
            python3 "$SCRIPT_DIR/scripts/gen_config.py" \
                --sub-url "$SUB_URL" --out "$MIHOMO_CONF_DIR/config.yaml.new" \
                --mixed-port "$MIXED_PORT" --api-port "$API_PORT" --dns-port "$DNS_PORT" \
                --probe-port-base "$PROBE_PORT_BASE" --openai-group "$OPENAI_GROUP" \
                --max-nodes "${MAX_NODES:-60}" --mihomo-bin "$MIHOMO_BIN" \
                --mihomo-conf-dir "$MIHOMO_CONF_DIR" \
                || die "更新失败"
            mv "$MIHOMO_CONF_DIR/config.yaml.new" "$MIHOMO_CONF_DIR/config.yaml"
            if [ "$SHADOW" != "1" ]; then
                systemctl --user restart "$UNIT_MIHOMO"
                ok "mihomo 已重启"
            fi
            ok "订阅更新完成"
            ;;
        controller)
            [ -f "$KIT_ENV" ] || die "未安装（先跑完整 install）"
            # shellcheck disable=SC1090
            . "$KIT_ENV"
            install_controller || die "controller 安装失败"
            ;;
        desktop)
            [ -f "$KIT_ENV" ] || die "未安装（先跑完整 install）"
            # shellcheck disable=SC1090
            . "$KIT_ENV"
            IS_GNOME=1 SHADOW=0 install_desktop_inject
            ;;
        *)
            die "未知子命令: $ACTION（可用: install/update/controller/desktop）"
            ;;
    esac
}

main

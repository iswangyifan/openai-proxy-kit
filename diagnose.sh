#!/usr/bin/env bash
# diagnose.sh — openai-proxy-kit 诊断排查链
# 按依赖顺序逐层检查，输出 PASS/FAIL/WARN 与修复建议
#
# 用法:
#   ./diagnose.sh                 # 基础链路
#   ./diagnose.sh --pid 1234      # 附加: 检查指定进程的代理环境变量
#                             #   （VSCode/Codex 插件不读系统代理、只认环境变量的核心坑）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
. "$SCRIPT_DIR/scripts/common.sh"

KIT_ENV="${KIT_ENV:-$HOME/.local/share/openai-proxy-kit/kit.env}"
if [ -f "$KIT_ENV" ]; then
    # shellcheck disable=SC1090
    load_kit_env || die "kit.env 加载异常"
fi
# 关键变量兜底（kit.env 缺失/损坏时仍可诊断默认端口）
: "${MIXED_PORT:=7890}" "${API_PORT:=9090}" "${PROBE_PORT_BASE:=17900}"
: "${OPENAI_GROUP:=OpenAI 自动选择}"
: "${UNIT_MIHOMO:=mihomo-kit.service}" "${UNIT_CONTROLLER:=opk-route.service}"
: "${UNIT_GUARD_TIMER:=proxy-guard-kit.timer}" "${UNIT_GUARD_SERVICE:=proxy-guard-kit.service}"
: "${MIHOMO_CONF_DIR:=$HOME/.config/mihomo}"

PASS=0; FAILS=0; WARNS=0
pass() { ok "$*"; PASS=$((PASS+1)); }
fail() { err "$*"; FAILS=$((FAILS+1)); }
warnf() { warn "$*"; WARNS=$((WARNS+1)); }

CHECK_PID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --pid) CHECK_PID="$2"; shift 2 ;;
        *) die "未知参数: $1（支持 --pid <进程号>）" ;;
    esac
done

SAVED_MODE=rule
if [ -f "$KIT_ENV" ] && [ -f "$MIHOMO_CONF_DIR/config.yaml" ]; then
    SAVED_MODE=$(python3 "$SCRIPT_DIR/scripts/mihomo_mode.py" --state "$KIT_ENV" get 2>/dev/null) || SAVED_MODE=invalid
fi
[ "$SAVED_MODE" != invalid ] || fail "核心配置的 mode 无效或不受支持"

phase "诊断 openai-proxy-kit（mihomo=$MIXED_PORT API=$API_PORT probe=$PROBE_PORT_BASE+ mode=$SAVED_MODE）"

# ---------- 1. 服务状态 ----------
echo "── 1. systemd 服务 ──"
if systemctl --user is-active --quiet "$UNIT_MIHOMO" 2>/dev/null; then
    pass "$UNIT_MIHOMO: active"
elif systemctl --user list-unit-files "$UNIT_MIHOMO" 2>/dev/null | grep -q "$UNIT_MIHOMO"; then
    fail "$UNIT_MIHOMO: $(systemctl --user is-active "$UNIT_MIHOMO" 2>/dev/null)（journalctl --user -u $UNIT_MIHOMO -n 30）"
else
    warnf "$UNIT_MIHOMO: 未安装（影子模式属正常）"
fi
if systemctl --user is-active --quiet "$UNIT_CONTROLLER" 2>/dev/null; then
    if [ "$SAVED_MODE" = direct ]; then fail "$UNIT_CONTROLLER: direct 模式下不应运行"; else pass "$UNIT_CONTROLLER: active"; fi
elif [ "$SAVED_MODE" = direct ]; then
    pass "$UNIT_CONTROLLER: direct 常驻模式下按预期停止"
elif systemctl --user list-unit-files "$UNIT_CONTROLLER" 2>/dev/null | grep -q "$UNIT_CONTROLLER"; then
    fail "$UNIT_CONTROLLER: $(systemctl --user is-active "$UNIT_CONTROLLER" 2>/dev/null)（journalctl --user -u $UNIT_CONTROLLER -n 30）"
else
    warnf "$UNIT_CONTROLLER: 未安装（影子模式或未装熔断器属正常）"
fi
if systemctl --user is-active --quiet "$UNIT_GUARD_TIMER" 2>/dev/null; then
    if [ "$SAVED_MODE" = direct ]; then fail "$UNIT_GUARD_TIMER: direct 模式下不应运行"; else pass "$UNIT_GUARD_TIMER: active"; fi
elif [ "$SAVED_MODE" = direct ]; then
    pass "$UNIT_GUARD_TIMER: direct 常驻模式下按预期停止"
else
    warnf "$UNIT_GUARD_TIMER: 未运行（非 GNOME/影子模式属正常）"
fi

# ---------- 2. 端口监听 ----------
echo "── 2. 端口监听 ──"
if port_in_use "$MIXED_PORT"; then pass "代理端口 $MIXED_PORT 监听中"; else fail "代理端口 $MIXED_PORT 未监听"; fi
if port_in_use "$API_PORT"; then pass "API 端口 $API_PORT 监听中"; else fail "API 端口 $API_PORT 未监听"; fi
probe_count=$(ss -tlnH "sport >= :$PROBE_PORT_BASE and sport <= :$((PROBE_PORT_BASE + 100))" 2>/dev/null | wc -l)
if [ "$probe_count" -gt 0 ]; then
    pass "probe 监听 ${probe_count} 个（${PROBE_PORT_BASE} 起）"
else
    fail "probe 监听 0 个（listeners 未生效? 配置重新生成: ./install.sh update）"
fi

# ---------- 3. API 与 OpenAI 组 ----------
echo "── 3. mihomo API 与 OpenAI 组 ──"
GROUP_ENC=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$OPENAI_GROUP" 2>/dev/null || echo "")
if GROUP_JSON=$(api_curl -fsS --noproxy '*' --max-time 5 "http://127.0.0.1:$API_PORT/proxies/$GROUP_ENC" 2>/dev/null); then
    NOW=$(echo "$GROUP_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('now','?'))" 2>/dev/null)
    CAND=$(echo "$GROUP_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('all') or []))" 2>/dev/null)
    pass "OpenAI 组「$OPENAI_GROUP」: 当前 $NOW / 候选 ${CAND:-0} 个"
    [ "${CAND:-0}" -gt 0 ] || fail "OpenAI 组候选为空"
else
    fail "API 无响应（mihomo 未运行?）"
fi
if CONFIG_JSON=$(api_curl -fsS --noproxy '*' --max-time 5 "http://127.0.0.1:$API_PORT/configs" 2>/dev/null); then
    RUNTIME_MODE=$(echo "$CONFIG_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('mode','?'))" 2>/dev/null)
    if [ "$RUNTIME_MODE" = "$SAVED_MODE" ]; then
        pass "磁盘/API 模式一致: $SAVED_MODE"
    else
        fail "磁盘/API 模式不一致: $SAVED_MODE/$RUNTIME_MODE"
    fi
else
    fail "无法回读核心运行模式"
fi

# ---------- 4. 连通性对比 ----------
echo "── 4. 连通性（经代理 vs 直连） ──"
P_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -x "http://127.0.0.1:$MIXED_PORT" https://chatgpt.com 2>/dev/null || echo 000)
D_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 --noproxy '*' https://chatgpt.com 2>/dev/null || echo 000)
case "$P_CODE" in
    200|301|302|307|308) pass "经本地核心 chatgpt.com: HTTP $P_CODE（模式 $SAVED_MODE）" ;;
    *) if [ "$SAVED_MODE" = direct ]; then
           info "direct 常驻下 chatgpt.com 不可达（HTTP $P_CODE，不代表代理链路故障）"
       else
           fail "经代理 chatgpt.com: HTTP $P_CODE（节点失效? 换节点或看熔断器日志）"
       fi ;;
esac
case "$D_CODE" in
    200|301|302|307|308) warnf "直连 chatgpt.com 也通（HTTP $D_CODE，本机网络环境特殊?）" ;;
    *) info "直连 chatgpt.com: HTTP $D_CODE（预期被墙，正常）" ;;
esac

# ---------- 5. probe 端口抽查 ----------
echo "── 5. probe 端口抽查（前 3 个候选） ──"
for i in 0 1 2; do
    p=$((PROBE_PORT_BASE + i))
    if port_in_use "$p"; then
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -x "http://127.0.0.1:$p" https://chatgpt.com 2>/dev/null || echo 000)
        case "$code" in
            200|301|302|307|308) pass "probe $p: HTTP $code" ;;
            *) warnf "probe $p: HTTP $code（候选 #$i 节点不可用，熔断器会隔离它）" ;;
        esac
    fi
done

# ---------- 6. 系统代理（GNOME） ----------
echo "── 6. GNOME 系统代理 ──"
if command -v gsettings >/dev/null 2>&1; then
    MODE=$(gsettings get org.gnome.system.proxy mode 2>/dev/null)
    H=$(gsettings get org.gnome.system.proxy.http host 2>/dev/null)
    P=$(gsettings get org.gnome.system.proxy.http port 2>/dev/null)
    if [ "$SAVED_MODE" = direct ]; then
        if [ "$MODE" = "'manual'" ] && [ "$H" = "'127.0.0.1'" ] && [ "$P" = "$MIXED_PORT" ]; then
            fail "direct 模式下系统代理仍指向本工具: 127.0.0.1:$MIXED_PORT"
        else
            pass "direct 模式下本工具未接管系统代理"
        fi
    elif [ "$MODE" = "'manual'" ] && [ "$H" = "'127.0.0.1'" ] && [ "$P" = "$MIXED_PORT" ]; then
        pass "系统代理: manual → 127.0.0.1:$MIXED_PORT"
    else
        fail "系统代理: mode=$MODE host=$H port=$P（期望 manual/127.0.0.1/$MIXED_PORT）"
        info "修复: systemctl --user start ${UNIT_GUARD_SERVICE:-proxy-guard-kit.service} 或手动 gsettings 设置"
    fi
else
    warnf "非 GNOME 环境，跳过"
fi

# ---------- 7. 指定进程的代理环境变量 ----------
if [ -n "$CHECK_PID" ]; then
    echo "── 7. 进程 $CHECK_PID 的代理环境变量 ──"
    if [ -r "/proc/$CHECK_PID/environ" ]; then
        if tr '\0' '\n' < "/proc/$CHECK_PID/environ" | grep -qi '^https\?_proxy='; then
            pass "进程带代理变量: $(tr '\0' '\n' < "/proc/$CHECK_PID/environ" | grep -i '^https\?_proxy=' | head -1)"
        elif [ "$SAVED_MODE" = direct ]; then
            pass "direct 模式下进程无代理变量，按预期直接联网"
        else
            fail "进程无代理变量（Codex/VSCode 等不读系统代理，需桌面启动器注入）"
            info "修复: ./install.sh desktop（应用需完全退出后从应用菜单重启）"
        fi
    else
        fail "无法读取 /proc/$CHECK_PID/environ（进程不存在或权限不足）"
    fi
fi

# ---------- 汇总 ----------
echo
info "汇总: $PASS 通过, $FAILS 失败, $WARNS 警告"
echo "  熔断器能力边界: 它只验证页面可达，探不出节点出口被 OpenAI 登录风控拒绝"
echo "  （现象: 网页打不开 + Cloudflare 403 地区限制）。此时手动换组内其他节点即可"
[ "$FAILS" -eq 0 ] && exit 0 || exit 1

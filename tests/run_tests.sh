#!/usr/bin/env bash
# run_tests.sh — openai-proxy-kit 单元测试（自包含，不联网、不写真实系统状态）
#
# 覆盖: gen_config.py 的筛选/报错/清洗/结构约定 + inject_desktop.py 注入/启停循环
#       + switch.sh 未安装拒绝 + gui.py headless 冒烟
# 需要本机有 mihomo 二进制（PATH 或 ~/.local/bin/mihomo）以启用 -t 校验；
# 没有则跳过 -t 相关断言，不影响其余用例。
#
# 用法: bash tests/run_tests.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(dirname "$SCRIPT_DIR")"
GEN="$KIT_ROOT/scripts/gen_config.py"
INJ="$KIT_ROOT/scripts/inject_desktop.py"

TEST_TMP_ROOT="${TMPDIR:-$KIT_ROOT/.artifacts/tmp}"
mkdir -p "$TEST_TMP_ROOT"
WORK=$(mktemp -d "$TEST_TMP_ROOT/kit-tests.XXXXXX") || exit 1
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAILED=0
ok_()   { printf '  [PASS] %s\n' "$*"; PASS=$((PASS+1)); }
fail_() { printf '  [FAIL] %s\n' "$*"; FAILED=$((FAILED+1)); }

MIHOMO_BIN="${MIHOMO_TEST_BIN:-$(command -v mihomo || true)}"
[ -z "$MIHOMO_BIN" ] && [ -x "$HOME/.local/bin/mihomo" ] && MIHOMO_BIN="$HOME/.local/bin/mihomo"

# geodata 从现用配置目录借（只读，供 -t 校验与 T6 免网络下载）；无则降级
GEODIR="$WORK/geodir"; mkdir -p "$GEODIR/ui"
for f in geosite.dat geoip.metadb country.mmdb; do
    [ -f "${MIHOMO_TEST_DATA_DIR:-$HOME/.config/mihomo}/$f" ] && \
        cp "${MIHOMO_TEST_DATA_DIR:-$HOME/.config/mihomo}/$f" "$GEODIR/" 2>/dev/null
done
echo placeholder > "$GEODIR/ui/.keep"   # 哑文件让 T6 跳过面板下载
if [ ! -f "$GEODIR/geosite.dat" ]; then
    printf '  [SKIP] 本机无 geosite.dat，-t 校验用例降级\n'
    MIHOMO_BIN=""
fi

mkconf() { # mkconf <out> <额外参数...>（固定基础参数 + mihomo -t 可用则启用）
    local out=$1; shift
    local extra=()
    [ -n "$MIHOMO_BIN" ] && extra+=(--mihomo-bin "$MIHOMO_BIN" --mihomo-conf-dir "$GEODIR")
    python3 "$GEN" --sub-url "$SUB" --out "$out" "${extra[@]}" "$@" >/dev/null 2>&1
}

# ---------- 合成订阅（自包含，不依赖真实机场数据） ----------
SUB="$WORK/sub.yaml"
python3 - "$SUB" <<'PYEOF'
import sys, yaml
nodes = [
    {"name": "日本JP-HY2",  "type": "ss", "server": "1.1.1.1", "port": 443, "cipher": "aes-128-gcm", "password": "a"},
    {"name": "韩国KR-HY2",  "type": "ss", "server": "1.1.1.2", "port": 443, "cipher": "aes-128-gcm", "password": "a"},
    {"name": "美国US-01",    "type": "ss", "server": "1.1.1.3", "port": 443, "cipher": "aes-128-gcm", "password": "a"},
    {"name": "台湾TW-GPT",   "type": "ss", "server": "1.1.1.4", "port": 443, "cipher": "aes-128-gcm", "password": "a"},
    {"name": "香港HK-01",    "type": "ss", "server": "1.1.1.5", "port": 443, "cipher": "aes-128-gcm", "password": "a"},
    # 重名（第二个清洗时应变 -2）
    {"name": "日本JP-HY2",  "type": "ss", "server": "1.1.1.6", "port": 443, "cipher": "aes-128-gcm", "password": "a"},
    # 空名 / 超长名（应被丢弃）
    {"name": "",             "type": "ss", "server": "1.1.1.7", "port": 443, "cipher": "aes-128-gcm", "password": "a"},
    {"name": "x" * 130,     "type": "ss", "server": "1.1.1.8", "port": 443, "cipher": "aes-128-gcm", "password": "a"},
]
yaml.dump({"proxies": nodes}, open(sys.argv[1], "w"), allow_unicode=True, sort_keys=False)
PYEOF

BADSUB="$WORK/bad.txt"
printf 'ss://bm90LWEtY2xhc2gtc3Vi@9.9.9.9:443#fake\n' > "$BADSUB"

echo "== gen_config.py =="

# T1 基础生成 + 结构约定
if mkconf "$WORK/t1.yaml"; then
    python3 - "$WORK/t1.yaml" <<'PYEOF' && ok_ "T1 基础生成 + probe 排序约定（共享节点排序与探测映射一致）" || fail_ "T1 结构断言"
import sys, yaml
c = yaml.safe_load(open(sys.argv[1]))
names = sorted({p["name"] for p in c["proxies"]})
# 8 条原始: 5 正常 + 1 重名(清洗为 -2 保留) + 1 空名(丢弃) + 1 超长(丢弃) => 6 个
assert len(c["proxies"]) == 6, c["proxies"]
assert any(n == "日本JP-HY2-2" for n in names), "重名未加 -2 后缀"
group = [g for g in c["proxy-groups"] if g["name"] == "OpenAI 自动选择"][0]
assert sorted(group["proxies"]) == names
assert [l["proxy"] for l in c["listeners"]] == names, "probe 排序与约定不一致"
assert [l["port"] for l in c["listeners"]] == [17900 + i for i in range(len(names))]
assert c["rules"][0] == "DOMAIN-SUFFIX,openai.com,OpenAI 自动选择"
assert c["rules"][-1] == "MATCH,DIRECT"
PYEOF
else
    fail_ "T1 生成失败"
fi

# T2 FILTER 筛选
if mkconf "$WORK/t2.yaml" --openai-filter 'HY2'; then
    python3 - "$WORK/t2.yaml" <<'PYEOF' && ok_ "T2 OPENAI_FILTER 筛选（组=匹配子集，静态 proxies 保留全部）" || fail_ "T2 筛选断言"
import sys, yaml, re
c = yaml.safe_load(open(sys.argv[1]))
group = [g for g in c["proxy-groups"] if g["name"] == "OpenAI 自动选择"][0]
matched = [p["name"] for p in c["proxies"] if re.search("HY2", p["name"])]
assert len(c["proxies"]) == 6, "静态 proxies 应保留全部"
assert sorted(group["proxies"]) == sorted(matched)
assert [l["proxy"] for l in c["listeners"]] == sorted(matched)
PYEOF
else
    fail_ "T2 筛选生成失败"
fi

# T3 FILTER 无匹配 → 报错且不产出
if mkconf "$WORK/t3.yaml" --openai-filter '不存在的XYZ' 2>/dev/null; then
    fail_ "T3 无匹配应报错退出"
else
    [ ! -f "$WORK/t3.yaml" ] && ok_ "T3 FILTER 无匹配报错且未产出文件" || fail_ "T3 不应产出文件"
fi

# T4 非 yaml 订阅 → 报错
if python3 "$GEN" --sub-url "$BADSUB" --out "$WORK/t4.yaml" >/dev/null 2>&1; then
    fail_ "T4 非 yaml 订阅应报错"
else
    ok_ "T4 非 Clash yaml 订阅正确拒绝"
fi

# T5 节点数上限截断
if mkconf "$WORK/t5.yaml" --max-nodes 3; then
    python3 - "$WORK/t5.yaml" <<'PYEOF' && ok_ "T5 max-nodes 截断（3 个）" || fail_ "T5 截断断言"
import sys, yaml
c = yaml.safe_load(open(sys.argv[1]))
assert len(c["proxies"]) == 3
assert len(c["listeners"]) == 3
PYEOF
else
    fail_ "T5 截断生成失败"
fi

# T6 免交互无 SUB_URL（conf 目录预置 geodata/ui，避免测试走网络下载）
# 注意1: 输出经文件中转再 grep —— install.sh 预期 exit 1，直接管道会被 pipefail 误判
# 注意2: 用户机上正运行 FlClash/Clash Verge 时，install.sh 阶段 1 冲突检测会先行拦截，
#        属环境不满足而非回归 → SKIP
mkdir -p "$WORK/t6bin" "$WORK/t6home"
for data in country.mmdb geosite.dat geoip.metadb; do touch "$GEODIR/$data"; done
printf '#!/bin/sh\nexit 0\n' > "$WORK/t6bin/systemctl"
printf '#!/bin/sh\nexit 1\n' > "$WORK/t6bin/pgrep"
printf '#!/bin/sh\necho Linger=yes\n' > "$WORK/t6bin/loginctl"
printf '#!/bin/sh\nexit 0\n' > "$WORK/t6bin/ss"
printf '#!/bin/sh\necho test-mihomo\n' > "$WORK/t6bin/mihomo"
chmod +x "$WORK/t6bin/"*
    ( cd "$KIT_ROOT" && PATH="$WORK/t6bin:$PATH" HOME="$WORK/t6home" \
         MIHOMO_BIN="$WORK/t6bin/mihomo" ASSUME_YES=1 SUB_URL= INSTALL_HOME="$WORK/t6home" \
         MIXED_PORT=17891 API_PORT=19091 DNS_PORT=1154 MIHOMO_CONF_DIR="$GEODIR" \
         bash install.sh > "$WORK/t6.out" 2>&1 )
    if grep -q '免交互模式必须' "$WORK/t6.out"; then
        ok_ "T6 免交互无 SUB_URL 正确拒绝"
    else
        fail_ "T6 免交互拒绝消息未出现（输出: $(tail -1 "$WORK/t6.out" 2>/dev/null)）"
    fi

echo "== inject_desktop.py =="
DT="$WORK/desktop"; mkdir -p "$DT/apps" "$DT/backup"
cat > "$DT/apps/app.desktop" <<'EOF'
[Desktop Entry]
Name=App
Exec=/usr/bin/app %F

[Desktop Action win]
Name=W
Exec=/usr/bin/app --win
EOF

python3 "$INJ" inject --desktop-file "$DT/apps/app.desktop" --proxy http://127.0.0.1:17890 \
    --user-apps "$DT/apps" --backup-dir "$DT/backup" >/dev/null 2>&1 \
  && grep -q '# openai-proxy-kit injected' "$DT/apps/app.desktop" \
  && [ "$(grep -c 'Exec=env http_proxy=' "$DT/apps/app.desktop")" = "2" ] \
  && ok_ "T7 注入（主 Exec + Action Exec 双注入）" || fail_ "T7 注入"

# 重复注入允许从原备份重建；幂等性由内容与恢复记录证明，不依赖日志措辞。
if cp "$DT/apps/app.desktop" "$DT/injected-before.desktop" \
    && cp "$DT/backup/app.desktop.orig" "$DT/original-before.desktop" \
    && cp "$DT/backup/manifest.json" "$DT/manifest-before.json" \
    && python3 "$INJ" inject --desktop-file "$DT/apps/app.desktop" --proxy http://127.0.0.1:17890 \
        --user-apps "$DT/apps" --backup-dir "$DT/backup" >/dev/null 2>&1 \
    && cmp -s "$DT/injected-before.desktop" "$DT/apps/app.desktop" \
    && cmp -s "$DT/original-before.desktop" "$DT/backup/app.desktop.orig" \
    && cmp -s "$DT/manifest-before.json" "$DT/backup/manifest.json"; then
    ok_ "T8 幂等（重复注入后内容、备份与恢复记录不变）"
else
    fail_ "T8 幂等（重复注入失败或内容、备份、恢复记录发生变化）"
fi

python3 "$INJ" restore --user-apps "$DT/apps" --backup-dir "$DT/backup" >/dev/null 2>&1 \
  && diff -q "$DT/backup/app.desktop.orig" "$DT/apps/app.desktop" >/dev/null \
  && ok_ "T9 还原（与原文件一致）" || fail_ "T9 还原"

# T10-T12: disable/enable 启停循环（日常开关语义，restore 后 fixture 已还原，先重新注入）
python3 "$INJ" inject --desktop-file "$DT/apps/app.desktop" --proxy http://127.0.0.1:17890 \
    --user-apps "$DT/apps" --backup-dir "$DT/backup" >/dev/null 2>&1
python3 "$INJ" disable --user-apps "$DT/apps" --backup-dir "$DT/backup" >/dev/null 2>&1 \
  && diff -q "$DT/backup/app.desktop.orig" "$DT/apps/app.desktop" >/dev/null \
  && ok_ "T10 disable（注入版还原为原版）" || fail_ "T10 disable"
python3 -c "import json; m=json.load(open('$DT/backup/manifest.json')); assert len(m['injected'])==1" 2>/dev/null \
  && ok_ "T10b disable 后 manifest 保留（enable 可恢复）" || fail_ "T10b manifest 应保留"
python3 "$INJ" enable --proxy http://127.0.0.1:17890 --user-apps "$DT/apps" --backup-dir "$DT/backup" >/dev/null 2>&1 \
  && grep -q '# openai-proxy-kit injected' "$DT/apps/app.desktop" \
  && ok_ "T11 enable（按 manifest 重新注入）" || fail_ "T11 enable"
python3 "$INJ" disable --user-apps "$DT/apps" --backup-dir "$DT/backup" >/dev/null 2>&1 \
  && diff -q "$DT/backup/app.desktop.orig" "$DT/apps/app.desktop" >/dev/null \
  && ok_ "T12 disable 幂等（重复停用安全）" || fail_ "T12 幂等"

echo "== switch.sh / gui.py =="

# T13 未安装时 switch.sh 正确拒绝
if KIT_ENV="$WORK/nonexistent.env" bash "$KIT_ROOT/scripts/switch.sh" status >/dev/null 2>&1; then
    fail_ "T13 switch.sh 未安装应报错"
else
    ok_ "T13 switch.sh 未安装时正确拒绝"
fi

# T14 gui.py headless 冒烟（需 GTK4；只 import + 只读查询，不实例化窗口）
if python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null; then
    cat > "$WORK/kit.env" <<'EOF'
INSTALL_HOME='/tmp/kit-gui-test-home'
MIXED_PORT='17890'
API_PORT='19090'
OPENAI_GROUP='测试组'
UNIT_MIHOMO='mihomo-unit-x.service'
UNIT_CONTROLLER='controller-x.service'
UNIT_GUARD_TIMER='guard-x.timer'
EOF
    if KIT_ENV="$WORK/kit.env" python3 -c "
import sys; sys.path.insert(0, '$KIT_ROOT/scripts')
import gui
assert gui.runtime.MIXED_PORT == '17890' and gui.UNITS['mihomo'] == 'mihomo-unit-x.service'
st = gui.query_status()   # 只读: 不存在的 unit/API 端口，无副作用
assert st['services']['mihomo'] is False
assert st['group_now'] is None
assert isinstance(st['nodes'], list)
" 2>/dev/null; then
        ok_ "T14 gui.py kit.env 解析 + 状态查询（headless 无副作用）"
    else
        fail_ "T14 gui.py 冒烟"
    fi
else
    printf '  [SKIP] T14 无 GTK4/libadwaita，gui.py 冒烟跳过\n'
fi

echo
if [ "$FAILED" -eq 0 ]; then
    printf '全部通过: %d/%d\n' "$PASS" "$((PASS))"
    exit 0
else
    printf '失败 %d 项, 通过 %d 项\n' "$FAILED" "$PASS"
    exit 1
fi

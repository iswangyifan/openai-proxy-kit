#!/usr/bin/env bash
# common.sh — openai-proxy-kit 公共函数库
# 供 install.sh / uninstall.sh / diagnose.sh source 使用

# ---------- 输出 ----------
if [ -t 1 ]; then
    C_INFO=$'\e[1;34m'; C_OK=$'\e[1;32m'; C_WARN=$'\e[1;33m'; C_ERR=$'\e[1;31m'; C_DIM=$'\e[2m'; C_OFF=$'\e[0m'
else
    C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi

info()  { printf '%s[INFO]%s %s\n'  "$C_INFO" "$C_OFF" "$*"; }
ok()    { printf '%s[ OK ]%s %s\n'  "$C_OK"   "$C_OFF" "$*"; }
warn()  { printf '%s[WARN]%s %s\n'  "$C_WARN" "$C_OFF" "$*"; }
err()   { printf '%s[FAIL]%s %s\n'  "$C_ERR"  "$C_OFF" "$*" >&2; }
die()   { err "$@"; exit 1; }
phase() { printf '\n%s========== %s ==========%s\n' "$C_INFO" "$*" "$C_OFF"; }

# ---------- 交互 ----------
ASSUME_YES="${ASSUME_YES:-0}"

confirm() { # confirm "提示语"  → 返回 0(yes)/1(no)
    [ "$ASSUME_YES" = "1" ] && return 0
    local prompt="${1:-确认?} [y/N] "
    read -r -p "$prompt" REPLY </dev/tty 2>/dev/null || read -r -p "$prompt" REPLY
    [[ "$REPLY" =~ ^[Yy] ]]
}

# ---------- 端口 ----------
port_in_use() { # port_in_use <port> → 0=占用 1=空闲
    ss -tlnH "sport = :$1" 2>/dev/null | grep -q .
}

port_user() { # port_user <port> → 占用进程描述（可能为空）
    ss -tlnp "sport = :$1" 2>/dev/null | grep -oE 'users:\(\("[^"]+"' | head -1 | sed 's/users:(("//'
}

# ---------- 模板渲染 ----------
_escape_sed() { printf '%s' "$1" | sed -e 's/[&|]/\\&/g'; }

render_template() { # render_template <tmpl> <out> VAR=val [VAR=val...]
    local tmpl=$1 out=$2; shift 2
    cp "$tmpl" "$out"
    local kv var val
    for kv in "$@"; do
        var="${kv%%=*}"; val="${kv#*=}"
        sed -i "s|@${var}@|$(_escape_sed "$val")|g" "$out"
    done
}

# ---------- 下载（直连 → ghproxy 镜像回退） ----------
GH_MIRROR="${GH_MIRROR:-https://ghproxy.cn/}"

dl() { # dl <url> <outfile>  （仅对 github.com URL 尝试镜像回退）
    local url=$1 out=$2
    if curl -fSL --connect-timeout 10 --max-time 300 -o "$out" "$url"; then
        return 0
    fi
    case "$url" in
        https://github.com/*|https://raw.githubusercontent.com/*|https://objects.githubusercontent.com/*|https://release.githubusercontent.com/*)
            warn "直连失败，尝试镜像: $GH_MIRROR"
            curl -fSL --connect-timeout 10 --max-time 300 -o "$out" "${GH_MIRROR}${url}" || return 1
            ;;
        *) return 1 ;;
    esac
}

# ---------- 环境探测 ----------
is_gnome() { command -v gsettings >/dev/null 2>&1 && command -v gnome-shell >/dev/null 2>&1; }

node_major() { # 输出 node 主版本号；无 node 输出 0
    command -v node >/dev/null 2>&1 || { echo 0; return; }
    node -v 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/'
}

have_pyyaml() { python3 -c "import yaml" >/dev/null 2>&1; }

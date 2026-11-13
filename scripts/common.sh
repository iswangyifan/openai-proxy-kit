#!/usr/bin/env bash
# common.sh — openai-proxy-kit 公共函数库
# 供 install.sh / uninstall.sh / diagnose.sh source 使用

COMMON_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
umask 077
load_kit_env() {
    local assignments
    assignments=$(python3 "$COMMON_SCRIPTS/kit_state.py" shell "$KIT_ENV") || return 1
    # 输出仅包含白名单变量的安全编码赋值，绝不执行原文件内容。
    eval "$assignments"
}

lock_kit() {
    [ "${OPK_LOCK_HELD:-}" = "$KIT_ENV" ] && return 0
    mkdir -p "$(dirname "$KIT_ENV")"
    exec 9>>"${KIT_ENV}.lock"
    flock -n 9 || die "另一个配置/启停操作正在进行，请稍后重试"
    export OPK_LOCK_HELD="$KIT_ENV"
}

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
GH_MIRROR="${GH_MIRROR:-}"

dl() { # dl <url> <outfile>  （仅对 github.com URL 尝试镜像回退）
    local url=$1 out=$2
    if curl -fSL --connect-timeout 10 --max-time 300 -o "$out" "$url"; then
        return 0
    fi
    case "$url" in
        https://github.com/*|https://raw.githubusercontent.com/*|https://objects.githubusercontent.com/*|https://release.githubusercontent.com/*)
            [ -n "$GH_MIRROR" ] || return 1
            warn "直连失败，使用显式指定的镜像；可执行文件仍须校验"
            curl -fSL --connect-timeout 10 --max-time 300 -o "$out" "${GH_MIRROR}${url}" || return 1
            ;;
        *) return 1 ;;
    esac
}

# ---------- 环境探测 ----------
is_gnome() { command -v gsettings >/dev/null 2>&1 && command -v gnome-shell >/dev/null 2>&1; }

have_pyyaml() { python3 -c "import yaml" >/dev/null 2>&1; }

# 密钥经 stdin 传给 curl，不出现在命令行参数中。
api_curl() {
    if [ -n "${MIHOMO_SECRET:-}" ]; then
        printf 'header = "Authorization: Bearer %s"\n' "$MIHOMO_SECRET" | curl --config - "$@"
    else
        curl "$@"
    fi
}

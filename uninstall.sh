#!/usr/bin/env bash
# 统一清理路径；保留用户私有配置、备份和已有第三方部署。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
args=()
if [ "${ASSUME_YES:-0}" = 1 ] || [ "${1:-}" = -y ]; then args+=(--yes); fi
exec /usr/bin/python3 "$SCRIPT_DIR/scripts/package_cleanup.py" "${args[@]}"

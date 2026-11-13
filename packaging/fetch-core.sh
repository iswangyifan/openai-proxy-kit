#!/usr/bin/env bash
# 固定版本与可信摘要；验证完成前不执行下载内容。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec /usr/bin/python3 "$ROOT/packaging/core_assets.py" fetch

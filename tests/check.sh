#!/bin/bash
# 不安装软件、不修改真实代理；真实核心仅在隔离临时配置中运行。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
for script in install.sh uninstall.sh diagnose.sh scripts/*.sh packaging/*.sh packaging/prerm; do bash -n "$script"; done
/usr/bin/python3 packaging/check_repository.py
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py' -v
MIHOMO_TEST_BIN="$ROOT/.build-cache/core/mihomo" bash tests/run_tests.sh
dbus-run-session -- /usr/bin/python3 tests/integration_smoke.py -v
if command -v xvfb-run >/dev/null; then
    xvfb-run -a dbus-run-session --config-file=tests/dbus-isolated.conf -- env GIO_USE_VFS=local GTK_A11Y=none ADW_DISABLE_PORTAL=1 GDK_BACKEND=x11 /usr/bin/python3 tests/desktop_smoke.py
elif [ -n "${DISPLAY:-}" ]; then
    dbus-run-session --config-file=tests/dbus-isolated.conf -- env GIO_USE_VFS=local GTK_A11Y=none ADW_DISABLE_PORTAL=1 GDK_BACKEND=x11 /usr/bin/python3 tests/desktop_smoke.py
else
    echo '无图形测试环境，GUI 验收未完成，请安装 xvfb' >&2
    exit 1
fi

if [ -f "dist/openai-proxy-kit_$(cat VERSION)_amd64.deb" ]; then
    /usr/bin/python3 packaging/verify_package.py
    (cd dist && sha256sum -c SHA256SUMS)
fi

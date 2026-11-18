#!/usr/bin/env bash
# 只构建，不安装、不启动用户服务。MIHOMO_BIN 可指定已经验证的核心。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="$(cat VERSION)"
ARCH="$(dpkg --print-architecture)"
MIHOMO_BIN="${MIHOMO_BIN:-$ROOT/.build-cache/core/mihomo}"
[ -x "$MIHOMO_BIN" ] || { echo '请用 MIHOMO_BIN 指定当前架构的 mihomo 核心' >&2; exit 1; }
/usr/bin/python3 packaging/core_assets.py origin "$MIHOMO_BIN" >/dev/null
MIHOMO_BIN="$MIHOMO_BIN" /usr/bin/python3 packaging/audit_core_licenses.py verify
mkdir -p build
STAGE="$(mktemp -d "$ROOT/build/deb.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/usr/lib/openai-proxy-kit"
mkdir -p "$APP/scripts" "$APP/templates" "$APP/assets" "$APP/libexec" \
    "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/openai-proxy-kit"
cp scripts/*.py scripts/*.sh "$APP/scripts/"
cp templates/* "$APP/templates/"
cp -R assets/. "$APP/assets/"
cp install.sh diagnose.sh uninstall.sh VERSION "$APP/"
touch "$APP/packaged"
install -m 0755 "$MIHOMO_BIN" "$APP/libexec/mihomo"
install -m 0755 packaging/openai-proxy-kit "$STAGE/usr/bin/openai-proxy-kit"
install -m 0644 packaging/openai-proxy-kit.desktop "$STAGE/usr/share/applications/io.github.opk.Console.desktop"
cp assets/icons/*.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/"
cp LICENSE NOTICE packaging/MIHOMO-LICENSE "$STAGE/usr/share/doc/openai-proxy-kit/"
cp -R third_party/mihomo-licenses "$STAGE/usr/share/doc/openai-proxy-kit/MIHOMO-DEPENDENCIES"
/usr/bin/python3 packaging/core_assets.py origin "$APP/libexec/mihomo" > "$STAGE/usr/share/doc/openai-proxy-kit/CORE-ORIGIN.json"
cp README.md SECURITY.md CONTRIBUTING.md CHANGELOG.md "$STAGE/usr/share/doc/openai-proxy-kit/"
cp -R docs "$STAGE/usr/share/doc/openai-proxy-kit/"
mkdir -p "$STAGE/usr/share/doc/openai-proxy-kit/packaging"
cp packaging/core.lock.json packaging/MIHOMO-LICENSE "$STAGE/usr/share/doc/openai-proxy-kit/packaging/"
cp packaging/copyright "$STAGE/usr/share/doc/openai-proxy-kit/copyright"
BUILD_DATE="$(LC_ALL=C date -u -R)"
cat > "$STAGE/usr/share/doc/openai-proxy-kit/changelog" <<EOF
openai-proxy-kit ($VERSION) unstable; urgency=medium

  * Harden proxy lifecycle, state parsing and management API authentication.
  * Provide a self-contained console and complete third-party source notices.

 -- openai-proxy-kit contributors <iswangyifan@users.noreply.github.com>  $BUILD_DATE
EOF
gzip -n -9 "$STAGE/usr/share/doc/openai-proxy-kit/changelog"
mkdir -p "$STAGE/usr/share/man/man1" "$STAGE/usr/share/lintian/overrides"
gzip -n -9 -c packaging/openai-proxy-kit.1 > "$STAGE/usr/share/man/man1/openai-proxy-kit.1.gz"
cp packaging/lintian-overrides "$STAGE/usr/share/lintian/overrides/openai-proxy-kit"
find "$APP" -type f -name '*.sh' -exec chmod 0755 {} +
install -m 0755 packaging/prerm "$STAGE/DEBIAN/prerm"
# sticky 目录仅用于 UID 自登记；维护脚本只信任文件 owner==文件名 UID，root不读取内容。
install -d -m 1777 "$STAGE/var/lib/openai-proxy-kit/users"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: openai-proxy-kit
Version: $VERSION
Section: net
Priority: optional
Architecture: $ARCH
Maintainer: openai-proxy-kit contributors <iswangyifan@users.noreply.github.com>
Homepage: https://github.com/iswangyifan/openai-proxy-kit
Depends: libc6 (>= 2.35), python3 (>= 3.10), python3-gi, python3-yaml, gir1.2-gtk-4.0 (>= 4.10), gir1.2-adw-1 (>= 1.4), gir1.2-gdkpixbuf-2.0, librsvg2-common, curl, systemd, iproute2, libglib2.0-bin, util-linux (>= 2.39), dbus, dconf-gsettings-backend, gsettings-desktop-schemas
Description: Linux proxy console for Codex and ChatGPT
 Native GTK4 dashboard, tray, live logs and project route engine.
 No additional runtime or external controller checkout is required.
EOF
find "$STAGE" -type f -exec chmod go-w {} +
find "$STAGE" -type d ! -path "$STAGE/var/lib/openai-proxy-kit/users" -exec chmod go-w {} +
chmod 0755 "$STAGE"
mkdir -p dist
dpkg-deb --root-owner-group --build "$STAGE" "dist/openai-proxy-kit_${VERSION}_${ARCH}.deb"
SOURCE_ARCHIVE=$(/usr/bin/python3 packaging/core_assets.py sources)
DEPENDENCIES_ARCHIVE="${SOURCE_ARCHIVE/source/dependencies}"
MIHOMO_BIN="$MIHOMO_BIN" /usr/bin/python3 packaging/audit_core_licenses.py sources "$ROOT/dist/$DEPENDENCIES_ARCHIVE"
cp third_party/mihomo-licenses/manifest.json dist/MIHOMO-COMPONENTS.json
/usr/bin/python3 packaging/core_assets.py origin "$MIHOMO_BIN" > dist/CORE-ORIGIN.json
(cd dist && sha256sum "openai-proxy-kit_${VERSION}_${ARCH}.deb" CORE-ORIGIN.json MIHOMO-COMPONENTS.json "$SOURCE_ARCHIVE" "$DEPENDENCIES_ARCHIVE" > SHA256SUMS)
echo "构建完成；未安装软件或启动代理。"

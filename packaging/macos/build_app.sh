#!/bin/bash
# QQ 分卷邮递 —— macOS .app 打包脚本
# 运行时零第三方依赖；本脚本只在构建期需要 PyInstaller 与系统自带的 sips/iconutil。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

APP_NAME="QQ分卷邮递"
ASSETS="$ROOT/assets"
BUILD_DIR="$ROOT/packaging/macos/build"
ICONSET="$BUILD_DIR/app.iconset"
ICNS="$ASSETS/app.icns"
DIST="$ROOT/dist_mac"
WORK="$BUILD_DIR/pyinstaller"
APP="$DIST/$APP_NAME.app"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "错误：此脚本只能在 macOS 上运行。" >&2
    exit 1
fi

if ! python3 -c "import PyInstaller" >/dev/null 2>&1; then
    echo "错误：未检测到 PyInstaller。请先执行：python3 -m pip install pyinstaller" >&2
    exit 1
fi

if [[ ! -f "$ASSETS/app.png" ]]; then
    echo "错误：缺少 $ASSETS/app.png，无法生成图标。" >&2
    exit 1
fi

echo "==> 1/3 从 app.png 生成 app.icns"
rm -rf "$BUILD_DIR"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
    double=$((size * 2))
    sips -z "$size" "$size" "$ASSETS/app.png" \
        --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    sips -z "$double" "$double" "$ASSETS/app.png" \
        --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" --out "$ICNS"
echo "    $ICNS"

echo "==> 2/3 PyInstaller 打包（--windowed --onedir）"
# --add-data 的相对源路径会按 --specpath 解析，所以这里必须用绝对路径。
rm -rf "$DIST"
python3 -m PyInstaller \
    --noconfirm --clean --onedir --windowed \
    --name "$APP_NAME" \
    --icon "$ICNS" \
    --add-data "$ASSETS:assets" \
    --distpath "$DIST" \
    --workpath "$WORK" \
    --specpath "$BUILD_DIR" \
    launch_gui.pyw

echo "==> 3/3 检查产物"
if [[ ! -d "$APP" ]]; then
    echo "错误：未找到预期的 .app：$APP" >&2
    find "$DIST" -maxdepth 2 -print >&2
    exit 1
fi

echo "完成：$APP"
echo "自检：  \"$APP/Contents/MacOS/$APP_NAME\" --selftest"
echo "首次打开若被 Gatekeeper 拦截，可执行："
echo "  xattr -dr com.apple.quarantine \"$APP\""
echo "注意：产物未做 Apple 签名/公证；设置写在 ~/Library/Application Support/QQVolMail/ 。"

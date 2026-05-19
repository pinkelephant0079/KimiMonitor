#!/bin/bash
# KimiMonitor GitHub Release 打包脚本
# 用法: ./build_release.sh [版本号]

set -e

# 版本号
VERSION="${1:-$(python3 -c 'from version import VERSION; print(VERSION)')}"
TAG="v${VERSION}"

echo "========================================"
echo "  KimiMonitor Release Builder"
echo "  版本: $VERSION"
echo "  Tag:  $TAG"
echo "========================================"
echo ""

# 检查环境
VENV_DIR="../kimi_monitor_env"
if [ ! -d "$VENV_DIR" ]; then
    echo "❌ 错误: 虚拟环境不存在于 $VENV_DIR"
    exit 1
fi

# 清理旧构建
echo "[1/5] 清理旧构建..."
rm -rf build dist
mkdir -p dist

# PyInstaller 打包
echo "[2/5] PyInstaller 打包..."
$VENV_DIR/bin/python -m PyInstaller \
    --clean --noconfirm \
    KimiMonitor.spec

# ad-hoc 签名
echo "[3/5] ad-hoc 签名..."
codesign --deep --force --sign - dist/KimiMonitor.app

# 验证签名
echo "     签名验证:"
codesign -dv dist/KimiMonitor.app 2>&1 | grep -E "Signature|Identifier" | head -3

# 创建 .dmg
echo "[4/5] 创建 .dmg..."
DMG_TEMP=$(mktemp -d)
cp -R dist/KimiMonitor.app "$DMG_TEMP/"
ln -s /Applications "$DMG_TEMP/Applications"

hdiutil create \
    -srcfolder "$DMG_TEMP" \
    -volname "KimiMonitor" \
    -fs HFS+ \
    -format UDZO \
    -o "dist/KimiMonitor-${VERSION}.dmg" \
    > /dev/null 2>&1

rm -rf "$DMG_TEMP"

# 计算校验和
echo "[5/5] 计算校验和..."
shasum -a 256 "dist/KimiMonitor-${VERSION}.dmg" > "dist/KimiMonitor-${VERSION}.dmg.sha256"

echo ""
echo "========================================"
echo "  ✅ 构建完成!"
echo "========================================"
echo ""
echo "产物:"
ls -lh dist/KimiMonitor-${VERSION}.dmg
echo ""
echo "SHA256:"
cat "dist/KimiMonitor-${VERSION}.dmg.sha256"
echo ""
echo "下一步:"
echo "  1. git tag $TAG"
echo "  2. git push origin $TAG"
echo "  3. 在 GitHub 上创建 Release，上传 dist/KimiMonitor-${VERSION}.dmg"
echo ""

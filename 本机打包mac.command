#!/bin/zsh
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
SCRIPT_DIR="${0:A:h}"
APP_DIR="$SCRIPT_DIR/apps/desktop"
OUT_DIR="$SCRIPT_DIR/local-release"
CONFIG="$SCRIPT_DIR/打包配置.sh"

fail() {
  printf "%b\n" "\n${RED}${BOLD}打包失败：$1${NC}\n"
  printf "%b\n" "${YELLOW}窗口已保留，请把上方错误发我。${NC}"
  read -r "?按回车键关闭窗口…"
  exit 1
}

echo "=== AI视频工作台 macOS 本机打包 ==="
cd "$SCRIPT_DIR" || fail "找不到项目目录：$SCRIPT_DIR"

[ -f "$CONFIG" ] && source "$CONFIG"
export NOTARY_PROFILE="${NOTARY_PROFILE:-miao}"
export CSC_NAME="${CSC_NAME:-AIMEI GROUP CO.,LTD. (7N4P7Y7APA)}"
export CSC_IDENTITY_AUTO_DISCOVERY=true
unset SKIP_NOTARIZE

command -v node >/dev/null 2>&1 || fail "未检测到 node，请先安装 Node.js"
command -v npm >/dev/null 2>&1 || fail "未检测到 npm，请先安装 Node.js"
command -v xcrun >/dev/null 2>&1 || fail "未检测到 Xcode Command Line Tools，请先安装：xcode-select --install"

if ! security find-identity -v -p codesigning | grep -F "Developer ID Application: $CSC_NAME" >/dev/null 2>&1; then
  fail "找不到签名证书：$CSC_NAME"
fi
if ! xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
  fail "找不到 Apple 公证钥匙串档案：$NOTARY_PROFILE。请先在终端执行：xcrun notarytool store-credentials \"$NOTARY_PROFILE\" --apple-id \"你的Apple ID\" --team-id \"7N4P7Y7APA\" --password \"App专用密码\""
fi

echo "[1/4] 检查依赖…"
if [ ! -d "$SCRIPT_DIR/node_modules" ]; then
  npm install || fail "npm install 失败"
else
  echo "依赖已存在，跳过 npm install"
fi

echo "[2/4] 类型检查 + 前端构建…"
npx vue-tsc --noEmit -p apps/desktop/tsconfig.web.json || fail "vue-tsc 检查失败"
npx tsc --noEmit -p apps/desktop/tsconfig.node.json || fail "tsc 检查失败"
npm run build --workspace=apps/desktop || fail "electron-vite build 失败"

echo "[3/4] 签名 + 公证 + 打包 dmg/zip…"
node -e "require('fs').rmSync(process.argv[1], { recursive: true, force: true })" "$APP_DIR/dist"
cd "$APP_DIR" || fail "找不到桌面端目录：$APP_DIR"
npx electron-builder --mac --publish never || fail "electron-builder mac 打包失败"

echo "[4/4] 复制产物到 local-release…"
mkdir -p "$OUT_DIR"
find "$APP_DIR/dist" -maxdepth 1 \( -name '*.dmg' -o -name '*.zip' -o -name 'latest-mac.yml' -o -name '*.blockmap' \) -type f -exec cp -f {} "$OUT_DIR/" \;

for dmg in "$OUT_DIR"/*.dmg; do
  [ -f "$dmg" ] || continue
  echo "[签名 DMG] $dmg"
  codesign --force --sign "Developer ID Application: $CSC_NAME" --timestamp "$dmg" || fail "DMG 签名失败：$dmg"
  echo "[公证 DMG] $dmg"
  xcrun notarytool submit "$dmg" --keychain-profile "$NOTARY_PROFILE" --wait || fail "DMG 公证失败：$dmg"
  xcrun stapler staple "$dmg" || fail "DMG 装订 ticket 失败：$dmg"
  xcrun stapler validate "$dmg" || fail "DMG 公证校验失败：$dmg"
done

printf "%b\n" "\n${GREEN}${BOLD}打包完成${NC}"
printf "%b\n" "产物目录：$OUT_DIR"
ls -lh "$OUT_DIR"
open "$OUT_DIR" >/dev/null 2>&1 || true
read -r "?按回车键关闭窗口…"

#!/usr/bin/env bash
# 双击这个文件即可启动（Finder 双击 .command 会打开终端窗口运行它）。
# 第一次双击如果被 macOS 拦下说"未知开发者"，右键选"打开"确认一次即可。
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

./start.sh
STATUS=$?

echo
if [ $STATUS -ne 0 ]; then
  echo "启动失败，退出码 $STATUS，看上面的报错信息。"
fi
read -n 1 -s -r -p "按任意键关闭这个窗口..."
echo

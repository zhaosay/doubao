#!/usr/bin/env bash
# 一键启动 AI视频工作台：装依赖 + 建/迁移数据库 + 建 Python venv + 拉起 Electron。
# 用法：终端里 ./start.sh，或者直接双击同目录下的「启动.command」。
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> 检查 Node 依赖"
if [ ! -d node_modules ]; then
  npm install
else
  echo "    已存在 node_modules，跳过 npm install（想强制重装就手动删了这个目录再跑）"
fi

echo "==> 同步数据库结构（Prisma migrate）"
(cd apps/desktop && npx prisma migrate deploy)

echo "==> 检查 Python 虚拟环境 (apps/ai-service/.venv)"
if [ ! -d apps/ai-service/.venv ]; then
  echo "    没找到 venv，创建一个"
  python3 -m venv apps/ai-service/.venv
fi

echo "==> 安装/更新 Python 依赖"
apps/ai-service/.venv/bin/pip install -q -r apps/ai-service/requirements.txt

echo "==> 一切就绪，启动 AI视频工作台（Electron 会自动拉起后端 FastAPI）"
npm run dev:desktop

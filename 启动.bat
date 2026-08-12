@echo off
chcp 65001 >nul
rem 双击这个文件即可启动（Windows 下 .bat 双击会直接开一个命令行窗口运行它，
rem 不像 macOS 的 .command 那样需要额外处理权限/安全提示）。
rem 逻辑跟 start.sh 完全对应：装 Node 依赖 -> 迁移数据库 -> 建/装 Python 虚拟环境 -> 启动。
cd /d "%~dp0"

echo ==^> 检查 Node 依赖
if not exist node_modules (
  call npm install
  if errorlevel 1 goto :fail
) else (
  echo     已存在 node_modules，跳过 npm install（想强制重装就手动删了这个目录再跑）
)

echo ==^> 同步数据库结构（Prisma migrate）
pushd apps\desktop
call npx prisma migrate deploy
if errorlevel 1 (
  popd
  goto :fail
)
popd

echo ==^> 检查 Python 虚拟环境 (apps\ai-service\.venv)
if not exist apps\ai-service\.venv (
  echo     没找到 venv，创建一个
  where py >nul 2>nul
  if errorlevel 1 (
    python -m venv apps\ai-service\.venv
  ) else (
    py -3 -m venv apps\ai-service\.venv
  )
  if errorlevel 1 (
    echo     创建虚拟环境失败，确认已安装 Python 3（勾选过"Add python.exe to PATH"）
    goto :fail
  )
)

echo ==^> 安装/更新 Python 依赖
call apps\ai-service\.venv\Scripts\pip.exe install -q -r apps\ai-service\requirements.txt
if errorlevel 1 goto :fail

echo ==^> 一切就绪，启动 AI视频工作台（Electron 会自动拉起后端 FastAPI）
call npm run dev:desktop
if errorlevel 1 goto :fail

goto :end

:fail
echo.
echo 启动失败，看上面的报错信息。
pause
exit /b 1

:end
echo.
pause

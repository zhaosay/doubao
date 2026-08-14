@echo off
setlocal EnableDelayedExpansion
title AI Video Workbench - Diagnostic Startup
cd /d "%~dp0"

set "LOGFILE=%~dp0diagnose.log"
echo AI Video Workbench diagnostic log - %date% %time% > "%LOGFILE%"
set STEP_FAILED=0

echo ============================================================
echo   AI Video Workbench - Diagnostic Startup
echo   Each step is marked [OK] / [WARN] / [FAIL]. If something is
echo   broken, scroll up to the first [FAIL] - that is the cause.
echo   Full log also written to diagnose.log next to this script.
echo ============================================================
echo.

echo [1/9] Checking Node.js ...
where node >nul 2>&1
if errorlevel 1 (
  echo   [FAIL] node not found. Install Node.js: https://nodejs.org/
  echo [FAIL] node not found >> "%LOGFILE%"
  set STEP_FAILED=1
  goto :SUMMARY
)
for /f "delims=" %%v in ('node --version 2^>nul') do echo   [OK] node %%v
for /f "delims=" %%v in ('npm --version 2^>nul') do echo   [OK] npm %%v
echo.

echo [2/9] Checking Python 3 ...
set "PYCMD="
py -3 --version >nul 2>&1
if not errorlevel 1 (
  set "PYCMD=py -3"
) else (
  python --version >nul 2>&1
  if not errorlevel 1 set "PYCMD=python"
)
if "!PYCMD!"=="" (
  echo   [FAIL] Python 3 not found. Install it and check "Add python.exe to PATH": https://www.python.org/
  echo [FAIL] Python 3 not found >> "%LOGFILE%"
  set STEP_FAILED=1
  goto :SUMMARY
)
for /f "delims=" %%v in ('!PYCMD! --version 2^>nul') do echo   [OK] %%v - command used: !PYCMD!
echo.

echo [3/9] Checking local claude CLI (Claude Code) - the usual cause of the "story generation 500"
set "CLAUDEPATH="
for %%c in (claude.cmd claude.exe claude) do (
  if "!CLAUDEPATH!"=="" (
    for /f "delims=" %%p in ('where %%c 2^>nul') do if "!CLAUDEPATH!"=="" set "CLAUDEPATH=%%p"
  )
)
if "!CLAUDEPATH!"=="" (
  echo   [WARN] claude / claude.cmd / claude.exe not found on PATH.
  echo          The default "local claude CLI" story generation mode needs it. Install with:
  echo            npm install -g @anthropic-ai/claude-code
  echo          then run "claude" once and log in.
  echo          Or switch "story generation provider" to the third-party API option in Settings.
  echo [WARN] claude CLI not found >> "%LOGFILE%"
) else (
  echo   [OK] found: !CLAUDEPATH!
  echo   Test call: running "claude -p --output-format json" the same way ai-service does
  echo Please reply with exactly: OK| "!CLAUDEPATH!" -p --output-format json
  if errorlevel 1 (
    echo   [FAIL] claude exited non-zero - the output right above is the real reason behind the 500
    echo          common causes: not logged in / session expired, no network, proxy blocking it
    echo [FAIL] claude CLI test call failed >> "%LOGFILE%"
  ) else (
    echo   [OK] claude CLI call succeeded - local claude setup is fine, the 500 is likely something else
  )
)
echo.

echo [4/9] Checking / installing Node dependencies (root, npm workspaces) ...
if not exist node_modules (
  echo   node_modules missing, running npm install - first run can take a while ...
  call npm install
  if errorlevel 1 (
    echo   [FAIL] npm install failed, see the output above
    set STEP_FAILED=1
    goto :SUMMARY
  )
) else (
  echo   [OK] node_modules already exists, skipping - delete it to force a reinstall
)
echo.

echo [5/9] Syncing database schema (Prisma migrate, apps\desktop) ...
pushd apps\desktop
call npx prisma migrate deploy
if errorlevel 1 (
  echo   [FAIL] prisma migrate deploy failed, see the output above
  popd
  set STEP_FAILED=1
  goto :SUMMARY
)
popd
echo   [OK]
echo.

echo [6/9] Checking / creating Python virtualenv (apps\ai-service\.venv) ...
if not exist apps\ai-service\.venv (
  echo   venv missing, creating one ...
  !PYCMD! -m venv apps\ai-service\.venv
  if errorlevel 1 (
    echo   [FAIL] failed to create the virtualenv
    set STEP_FAILED=1
    goto :SUMMARY
  )
) else (
  echo   [OK] .venv already exists
  echo        Note: the venv hardcodes the absolute path it was created at. If this
  echo        project folder was recently moved or copied, delete apps\ai-service\.venv
  echo        and rerun this script.
)
echo.

echo [7/9] Installing / updating Python dependencies ...
apps\ai-service\.venv\Scripts\pip.exe install -q -r apps\ai-service\requirements.txt
if errorlevel 1 (
  echo   [FAIL] pip install failed, see the output above - often a network / pip mirror issue
  set STEP_FAILED=1
  goto :SUMMARY
)
echo   [OK]
echo.

echo [8/9] Checking whether port 8000 is already in use (ai-service always binds to it) ...
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo   [WARN] Port 8000 is already in use - this is a common reason ai-service fails to start:
  netstat -ano | findstr ":8000" | findstr "LISTENING"
  echo          The last column is the PID; find it in Task Manager and close that process.
) else (
  echo   [OK] Port 8000 is free
)
echo.

echo [9/9] Checks done, starting AI Video Workbench (Electron will launch the FastAPI backend) ...
echo        The window header shows "backend ok/error". If it says error, check this
echo        terminal window for lines starting with [ai-service] - the real error is there.
echo.
call npm run dev:desktop
set APP_EXIT=%errorlevel%
echo App exit code: %APP_EXIT% >> "%LOGFILE%"
if not "%APP_EXIT%"=="0" (
  echo.
  echo [FAIL] App exited abnormally, exit code %APP_EXIT% - the output above is why
  set STEP_FAILED=1
)

:SUMMARY
echo.
echo ============================================================
if "%STEP_FAILED%"=="1" (
  echo   Result: at least one step FAILED or WARNED, scroll up to find it
) else (
  echo   Result: all checks passed
)
echo   Full log written to: %LOGFILE%
echo ============================================================
pause

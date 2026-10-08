@echo off
setlocal EnableDelayedExpansion

REM === Repo root is the folder holding this .cmd; the web workbench lives in ./studio ===
set "STUDIO=%~dp0studio"
set "PORT=8090"
set "URL=http://127.0.0.1:%PORT%/?demo=1"

echo ============================================================
echo   QuicStudio one-click launcher (frontend demo mode)
echo ============================================================
echo.

REM === 1. Locate Python: prefer 'py' launcher (avoids Win Store alias) ===
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  if exist "C:\Users\EDY\AppData\Local\Programs\Python" (
    for /d %%d in ("C:\Users\EDY\AppData\Local\Programs\Python\Python*") do (
      if exist "%%d\python.exe" set "PY=%%d\python.exe"
    )
  )
)
if not defined PY (
  echo [ERROR] Python not found. Install from python.org and tick "Add Python to PATH".
  echo          Then double-click this file again.
  pause
  exit /b 1
)
echo [1/3] Python: %PY%

REM === 2. Free port 8090 (kill leftover) ===
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":8090" ^| findstr "LISTENING"') do (
  taskkill /f /pid %%a >nul 2>nul
)

REM === 3. Start frontend preview (minimized window) ===
echo [2/3] Starting frontend (http://127.0.0.1:%PORT%/)...
start "QuicStudio-Frontend" /min "%PY%" "%STUDIO%\frontend\serve_preview.py" --host 127.0.0.1 --port %PORT%

REM === 4. Wait until ready, then open browser ===
echo [3/3] Waiting for server, then opening browser...
set "READY=0"
for /L %%i in (1,1,20) do (
  curl -s -o nul "http://127.0.0.1:%PORT%/" >nul 2>nul
  if not errorlevel 1 (
    set "READY=1"
    goto :open
  )
  timeout /t 1 >nul
)
:open
if "%READY%"=="1" (
  start "" "%URL%"
  echo.
  echo SUCCESS - UI opened in browser: %URL%
) else (
  echo.
  echo [WARN] Server did not come up in time. Check the "QuicStudio-Frontend" window.
  echo        Or open manually: %URL%
)

echo.
echo NOTE: Pure-frontend demo mode (?demo=1) - no backend needed; all consoles browsable.
echo STOP: Close the minimized "QuicStudio-Frontend" window to stop the server.
echo.
pause

@echo off
chcp 65001 >nul
title RoboData Workbench - Fetch Full SO-100 Dataset
set ROBODATA_SO100_EPISODES=0-49
echo.
echo   RoboData Workbench  -  Fetch full SO-100 dataset
echo   ============================================================
echo   Downloads all 50 episodes (32,068 frames, about 650 MB) from
echo   the pinned public source, verifying every file by SHA-256.
echo.
echo   Files already present are reused, so re-running is cheap.
echo   ============================================================
echo.
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo   [ERROR] Environment not found. Run the setup script first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m robodata fetch-sample
if errorlevel 1 (
  echo.
  echo   [ERROR] Download failed.
  echo   If you are behind a proxy, make sure it can reach huggingface.co,
  echo   or clear HTTP_PROXY / HTTPS_PROXY and retry.
  pause
  exit /b 1
)
echo.
echo   Done. Full SO-100 range 0-49 is now available on disk.
echo   Launch the workbench with the full-range entry to use it.
echo.
pause

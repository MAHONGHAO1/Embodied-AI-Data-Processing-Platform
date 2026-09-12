@echo off
chcp 65001 >nul
title RoboData Workbench - Full Range Launcher
set ROBODATA_HDF5_EPISODES=0-9
set ROBODATA_SO100_EPISODES=0-49
echo.
echo   Full data range mode
echo     - robomimic HDF5 : demo_0-9   (10 episodes /   531 frames)
echo     - SO-100         : episode 0-49 (50 episodes / 32,068 frames)
echo.
echo   Changing the range alters the input fingerprint, so existing
echo   batches must be re-imported.
echo   If SO-100 files are missing, run the fetch-full-data entry first.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 pause

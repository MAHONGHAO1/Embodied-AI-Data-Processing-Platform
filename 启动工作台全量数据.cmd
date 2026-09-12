@echo off
chcp 65001 >nul
title RoboData Workbench - Full Range Launcher
set ROBODATA_HDF5_EPISODES=0-9
echo.
echo   Full data range mode: demo_0-9 (10 episodes / 531 frames)
echo   Changing the range alters the input fingerprint, so existing
echo   batches must be re-imported.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 pause

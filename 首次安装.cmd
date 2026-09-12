@echo off
chcp 65001 >nul
title RoboData Workbench - First Time Setup
echo.
echo   RoboData Workbench  -  First time setup
echo   Starting setup wizard...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 pause

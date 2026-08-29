@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo Lossless Video Slicer - Windows Native Build
echo ============================================================
echo.
echo This script builds a real Windows x64 executable on this PC.
echo It does not require a preinstalled Python or FFmpeg.
echo.

where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo ERROR: powershell.exe was not found.
  echo Please send this screenshot to ChatGPT.
  pause
  exit /b 10
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo BUILD FAILED.
  echo Please send build_error.log to ChatGPT.
  pause
  exit /b %RC%
)

echo BUILD SUCCEEDED.
echo Output:
echo   %~dp0dist\LosslessVideoSlicer\LosslessVideoSlicer.exe
echo.
pause

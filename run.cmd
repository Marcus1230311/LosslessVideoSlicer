@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%CD%\.build\python\python.exe"
if not exist "%PYTHON%" (
  echo ERROR: Private Python runtime not found:
  echo   %PYTHON%
  echo.
  echo Run build.cmd once in this project folder first.
  pause
  exit /b 1
)

"%PYTHON%" -c "import os,sys; sys.path.insert(0, os.getcwd()); from app.main import run; run()"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo APP FAILED. Exit code: %RC%
  pause
)
exit /b %RC%

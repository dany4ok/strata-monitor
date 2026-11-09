@echo off
setlocal
set "APP=%~dp0strata-monitor.py"
if defined STRATA_MONITOR_PYTHON (
  start "" "%STRATA_MONITOR_PYTHON%" "%APP%"
  exit /b 0
)
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%APP%"
  exit /b 0
)
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw.exe "%APP%"
  exit /b 0
)
echo Python with PySide6 was not found. See README.md for setup.
pause

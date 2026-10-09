@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "src\web_console.py"
  goto :end
)

where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3.12 "src\web_console.py"
  goto :end
)

echo [ERROR] Python 3.12 was not found.
echo Install Python 3.12 or create .venv in this project first.
pause

:end
endlocal

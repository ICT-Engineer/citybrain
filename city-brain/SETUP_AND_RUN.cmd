@echo off
setlocal
cd /d "%~dp0"
title City Brain (standalone)
echo Stopping any previous City Brain on port 8300...
powershell -NoProfile -ExecutionPolicy Bypass -Command "foreach($p in 8300,8310,8320){Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"
if exist ".venv\Scripts\python.exe" goto run
echo First run: creating .venv and installing requirements...
where uv >nul 2>nul && (uv venv --python 3.12 .venv && uv pip install --python .venv\Scripts\python.exe -r requirements.txt || goto fail) && goto run
py -3.12 -m venv .venv || goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto fail
:run
echo.
echo City Brain (alone, built-in Twin and Guardian simulators)
echo   Cortex   : http://127.0.0.1:8300/city
echo   Timeline : http://127.0.0.1:8300/timeline
.venv\Scripts\python.exe run_local.py
pause
exit /b 0
:fail
echo ERROR: setup failed - see messages above.
pause
exit /b 1

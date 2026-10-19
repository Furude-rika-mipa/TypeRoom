@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel% equ 0 (
  python -m pip install --disable-pip-version-check --upgrade --target .deps -r requirements.txt
) else (
  py -3 -m pip install --disable-pip-version-check --upgrade --target .deps -r requirements.txt
)
if errorlevel 1 pause

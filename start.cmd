@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel% equ 0 (
  python launch.py --open
) else (
  py -3 launch.py --open
)
if errorlevel 1 pause

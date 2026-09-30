@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo ============================================================
echo   BTC Treasury mNAV (MSTR / ASST)
echo   %1 %2 %3
echo ============================================================
if exist ".venv\Scripts\python.exe" (".venv\Scripts\python.exe" btc_nav.py %*) else (python btc_nav.py %*)
echo.
pause

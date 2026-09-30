@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo ============================================================
echo   News Monitor - RSS collector (polling)
echo   Close this window (or Ctrl+C) to stop.
echo ============================================================
if exist ".venv\Scripts\python.exe" (".venv\Scripts\python.exe" rss_collector.py) else (python rss_collector.py)
echo.
echo [stopped]
pause

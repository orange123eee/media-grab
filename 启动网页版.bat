@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m media_grab.web --port 8765
pause

@echo off
title AnimeForge
cd /d "%~dp0"
echo.
echo  ========================================
echo    ANIMEFORGE  -  Starting up...
echo  ========================================
echo.
echo  Server: http://localhost:5000
echo  Admin login: admin / juste
echo  Press Ctrl+C to stop.
echo.

start cmd /c "ping -n 4 127.0.0.1 > nul && start http://localhost:5000"
python app.py

pause

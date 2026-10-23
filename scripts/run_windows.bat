@echo off
REM Runs the bot on Windows. !restart and !update come back automatically (exit code 42).
chcp 65001 >nul
cd /d "%~dp0\.."
title NUBTK Discord Bot

:loop
venv\Scripts\python.exe bot.py
set CODE=%errorlevel%
if %CODE%==42 (
  echo Restarting...
  timeout /t 2 >nul
  goto loop
)
if %CODE%==3 (
  echo.
  echo Config problem - fix .env or config.json, then run again.
  pause
  exit /b 3
)
echo.
echo Bot stopped or crashed (code %CODE%). Restarting in 15 seconds... (close window to stop)
timeout /t 15
goto loop

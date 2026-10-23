@echo off
REM One-time setup on Windows PC. Double-click or run from the project folder.
chcp 65001 >nul
cd /d "%~dp0\.."

where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version || (echo Python not found. Install Python 3.11+ from python.org and tick "Add to PATH". & pause & exit /b 1)

if not exist venv (
  echo Creating virtual environment...
  %PY% -m venv venv
)
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo  .env file created. Open it in Notepad and paste DISCORD_TOKEN, GUILD_ID, ADMIN_IDS.
  notepad .env
)
echo.
echo Setup done. Now run scripts\run_windows.bat
pause

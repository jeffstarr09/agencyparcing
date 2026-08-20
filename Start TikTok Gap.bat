@echo off
REM Double-click this file to start the app.
REM
REM The first run takes a minute while it sets itself up. After that it's quick.
REM Leave this black window open while you use the app; closing it quits.

cd /d "%~dp0"

echo.
echo   TikTok Gap
echo   ----------
echo.

where py >nul 2>&1
if %errorlevel%==0 (set PY=py -3) else (
  where python >nul 2>&1
  if %errorlevel%==0 (set PY=python) else (
    echo   Python isn't installed.
    echo.
    echo   Get it from  https://www.python.org/downloads/
    echo   During setup, tick "Add Python to PATH".
    echo   Then double-click this file again.
    echo.
    pause
    exit /b 1
  )
)

if not exist ".venv" (
  echo   First run - setting up ^(about a minute^)...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo   Could not create the environment.
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate.bat

python -c "import requests" >nul 2>&1
if errorlevel 1 (
  echo   Installing what it needs...
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo   Could not install the packages. Are you online?
    pause
    exit /b 1
  )
)

echo   Starting. Your browser should open in a second.
echo   Keep this window open while you use it.
echo.

python app.py

echo.
echo   The app has stopped.
pause

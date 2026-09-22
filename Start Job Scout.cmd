@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating the local Python environment...
    python -m venv .venv
    if errorlevel 1 goto error
)
".venv\Scripts\python.exe" -c "import flask, requests, bs4, protego, waitress" >nul 2>&1
if errorlevel 1 (
    echo Installing Job Scout dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto error
)
".venv\Scripts\python.exe" -m job_scout --open-browser
if errorlevel 1 goto error
exit /b 0
:error
echo.
echo Job Scout could not start. Review the error above.
echo Python 3.11 or newer must be installed and available as python.
pause
exit /b 1

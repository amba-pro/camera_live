@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [camera_live] Creating Python virtual environment...
    where py >nul 2>nul
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 goto error
)
echo [camera_live] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto error
echo [camera_live] Starting browser stream...
".venv\Scripts\python.exe" camera_live.py %*
if errorlevel 1 goto error
goto end
:error
echo.
echo [camera_live] Startup failed. Check Python installation and camera permissions.
pause
:end
endlocal

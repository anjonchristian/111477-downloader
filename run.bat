@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10+ from python.org and try again.
    pause
    exit /b 1
)

python -c "import requests, patchright" >nul 2>nul
if errorlevel 1 (
    echo Installing required packages...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

for /f "usebackq delims=" %%i in (`python -c "import sys,os;p=os.path.join(os.path.dirname(sys.executable),'pythonw.exe');print(p if os.path.exists(p) else '')"`) do set "PYTHONW=%%i"

if defined PYTHONW (
    start "" "%PYTHONW%" "%~dp0app.py"
) else (
    start "" python "%~dp0app.py"
)

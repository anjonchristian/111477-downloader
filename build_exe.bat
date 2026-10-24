@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10+ and try again.
    pause
    exit /b 1
)

echo Installing build dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo Building dist\111477-Downloader.exe ...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "111477-Downloader" ^
    --icon "assets\icon.ico" ^
    --add-data "assets;assets" ^
    --collect-all patchright ^
    --hidden-import patchright.sync_api ^
    app.py
if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Done: dist\111477-Downloader.exe
pause

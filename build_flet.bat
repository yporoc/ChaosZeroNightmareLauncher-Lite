@echo off
rem ============================================================
rem  czn-lite FLET GUI build script (PyInstaller, all deps bundled)
rem  usage:  build_flet.bat          -> onedir (recommended)
rem  output: dist\czn-lite-flet\czn-lite-flet.exe
rem  NOTE: config.json is copied next to the exe automatically.
rem  NOTE: this file MUST stay pure ASCII (cmd mis-parses UTF-8).
rem  NOTE: EXPERIMENTAL - Flet 1.0 desktop client binaries are
rem        downloaded to a local cache on first run; a machine
rem        without network needs that cache copied manually.
rem ============================================================
setlocal
cd /d "%~dp0"

echo === czn-lite FLET GUI build ===

where py >nul 2>nul
if errorlevel 1 (
    echo [x] Python launcher "py" not found. Install Python 3.12 first.
    pause
    exit /b 1
)

echo [1/3] ensuring pyinstaller...
py -m pip install --disable-pip-version-check --quiet pyinstaller
if errorlevel 1 (
    echo [x] pip install pyinstaller failed - check network / proxy
    pause
    exit /b 1
)

echo [2/3] building (this takes 1-3 minutes)...
set ARGS=--noconfirm --clean --windowed --name czn-lite-flet --collect-all flet --collect-all flet_desktop --collect-all flet_video --collect-all curl_cffi --exclude-module numpy --exclude-module customtkinter
py -m PyInstaller %ARGS% gui_flet.py
if errorlevel 1 (
    echo [x] build failed
    pause
    exit /b 1
)

echo [3/3] placing config files...
copy /y config.json dist\czn-lite-flet\ >nul
mkdir dist\czn-lite-flet\assets\videos >nul 2>nul
copy /y assets\videos\*.mp4 dist\czn-lite-flet\assets\videos\ >nul
ideos\*.mp4 dist\czn-lite-fletssets
ideos\ >nul
echo [+] DONE: dist\czn-lite-flet\czn-lite-flet.exe
echo     distribution = zip the dist folder and send it
pause

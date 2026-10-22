@echo off
rem ============================================================
rem  czn-lite FLET GUI build script (PyInstaller, all deps bundled)
rem  usage:  build_flet.bat          -> onedir (recommended)
rem  output: dist\czn-lite-flet\czn-lite-flet.exe
rem  NOTE: config.json is copied next to the exe automatically.
rem  NOTE: this file MUST stay pure ASCII (cmd mis-parses UTF-8).
rem  NOTE: OFFLINE BY DESIGN - the Flet 1.0 desktop client (flet-windows.zip)
rem        is bundled into _internal\flet_desktop\app\ so a clean machine never
rem        downloads it. Put the artifact at vendor\flet-windows.zip first:
rem          https://github.com/flet-dev/flet/releases/download/v1.0.3/flet-windows.zip
rem          sha256 b92a6905b948b9a565e2163d8a3c878cd0499a7997bc0ef37b7c4cac9eae6bff
rem ============================================================
setlocal
cd /d "%~dp0"

echo === czn-lite FLET GUI build ===

set FLET_VER=1.0.3
if not exist vendor\flet-windows.zip (
    echo [x] missing vendor\flet-windows.zip - the package would need network on first run.
    echo     download it from:
    echo       https://github.com/flet-dev/flet/releases/download/v%FLET_VER%/flet-windows.zip
    echo     and put it at vendor\flet-windows.zip, then run this script again.
    pause
    exit /b 1
)

where py >nul 2>nul
if errorlevel 1 (
    echo [x] Python launcher "py" not found. Install Python 3.12 first.
    pause
    exit /b 1
)

echo [1/4] checking the interpreter against requirements.txt...
rem PyInstaller bundles whatever this interpreter already has, so the floors
rem and the Flet pairing in requirements.txt have to be checked here -
rem otherwise they are a comment, not a package.
rem A missing or mismatched flet-desktop does NOT fail the build: PyInstaller
rem only warns, --add-data still creates _internal\flet_desktop\app, and the
rem check in step 4 passes. The frozen app dies at startup instead.
py -c "import certifi, flet, flet_desktop, flet_video, sys; from importlib.metadata import version as V; ok = tuple(int(x) for x in V('pillow').split('.'))[:2] >= (13, 0) and tuple(int(x) for x in V('certifi').split('.')) >= (2026, 10, 19) and all(V(p) == '%FLET_VER%' for p in ('flet', 'flet-desktop', 'flet-video')); sys.exit(0 if ok else 1)" >nul 2>nul
if errorlevel 1 (
    echo [x] this interpreter does not match requirements.txt:
    echo       pillow and certifi above their floors, and
    echo       flet, flet-desktop, flet-video all at v%FLET_VER%.
    echo     Fix it before building:  py -m pip install -r requirements.txt
    echo     Do not ship a package built on the wrong bundles.
    pause
    exit /b 1
)

echo [2/4] ensuring pyinstaller...
py -m pip install --disable-pip-version-check --quiet pyinstaller
if errorlevel 1 (
    echo [x] pip install pyinstaller failed - check network / proxy
    pause
    exit /b 1
)

echo [3/4] building (this takes 1-3 minutes)...
set ARGS=--noconfirm --clean --windowed --name czn-lite-flet --collect-all flet --collect-all flet_desktop --collect-all flet_video --collect-all curl_cffi --exclude-module numpy --exclude-module customtkinter --add-data "vendor\flet-windows.zip;flet_desktop/app"
py -m PyInstaller %ARGS% gui_flet.py
if errorlevel 1 (
    echo [x] build failed
    pause
    exit /b 1
)

echo [4/4] placing config files...
copy /y config.json dist\czn-lite-flet\ >nul
mkdir dist\czn-lite-flet\assets\videos >nul 2>nul
copy /y assets\videos\*.mp4 dist\czn-lite-flet\assets\videos\ >nul
if not exist dist\czn-lite-flet\_internal\flet_desktop\app\flet-windows.zip (
    echo [x] bundled client missing in dist - package is NOT offline-capable.
    pause
    exit /b 1
)
echo [+] bundled flet client: dist\czn-lite-flet\_internal\flet_desktop\app\flet-windows.zip
echo [+] DONE: dist\czn-lite-flet\czn-lite-flet.exe
echo     distribution = zip the dist folder and send it
pause

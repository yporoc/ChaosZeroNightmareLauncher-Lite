@echo off
rem ============================================================
rem  czn-lite GUI build script (PyInstaller, all deps bundled)
rem  usage:  build.bat          -> onedir (recommended: fast start,
rem                              lower antivirus false-positive rate)
rem          build.bat onefile  -> single exe
rem  output: dist\czn-lite-gui\czn-lite-gui.exe  (onedir)
rem          dist\czn-lite-gui.exe              (onefile)
rem  NOTE: config.json is copied next to the exe automatically.
rem  NOTE: this file MUST stay pure ASCII (cmd mis-parses UTF-8).
rem ============================================================
setlocal
cd /d "%~dp0"

echo === czn-lite GUI build ===

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
set ARGS=--noconfirm --clean --windowed --name czn-lite-gui --collect-all customtkinter --collect-all curl_cffi
if /i "%~1"=="onefile" set ARGS=%ARGS% --onefile
py -m PyInstaller %ARGS% gui.py
if errorlevel 1 (
    echo [x] build failed
    pause
    exit /b 1
)

echo [3/3] placing config files...
if /i "%~1"=="onefile" (
    copy /y config.json dist\ >nul
    echo [+] DONE: dist\czn-lite-gui.exe   (config.json must stay next to it)
) else (
    copy /y config.json dist\czn-lite-gui\ >nul
    echo [+] DONE: dist\czn-lite-gui\czn-lite-gui.exe
)
echo     distribution = zip the dist folder and send it
pause

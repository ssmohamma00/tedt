@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   Game For Sale - Windows installer builder
echo ============================================
echo.

rem --- 1) Find Python ----------------------------------------------------
rem   Tries PATH first (py launcher, then python), then falls back to
rem   scanning the usual install folders directly -- this matters
rem   because Python's own installer only adds it to PATH if the
rem   "Add python.exe to PATH" box was checked, and a shortcut launched
rem   straight from the Start menu works fine without that, which is a
rem   common source of confusion ("Python runs, but build.bat says
rem   it's missing").
set "PY="

where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3"

if "%PY%"=="" (
    where python >nul 2>nul
    if %errorlevel%==0 set "PY=python"
)

if "%PY%"=="" (
    echo "py"/"python" not on PATH yet - checking common install folders...
    set "FOUND_PY="
    for %%D in (
        "%LOCALAPPDATA%\Programs\Python\Python3*"
        "%ProgramFiles%\Python3*"
        "%ProgramFiles(x86)%\Python3*"
        "C:\Python3*"
    ) do (
        if not defined FOUND_PY (
            for /d %%G in (%%D) do (
                if not defined FOUND_PY if exist "%%G\python.exe" set "FOUND_PY=%%G\python.exe"
            )
        )
    )
    if defined FOUND_PY (
        set PY="!FOUND_PY!"
        echo Found Python at: !FOUND_PY!
    )
)

if "%PY%"=="" (
    echo [ERROR] Could not find a Python installation on this computer,
    echo either on PATH or in the usual install folders.
    echo.
    echo Install it from https://www.python.org/downloads/ and make sure to
    echo check "Add python.exe to PATH" on the very first install screen,
    echo then run this build.bat again. If Python IS installed but this
    echo still fails, open the Start menu, search "python", right-click it,
    echo choose "Open file location" to find its folder, and send that path
    echo to whoever is helping you with this script.
    pause
    exit /b 1
)

echo Using Python: %PY%
%PY% --version
echo.

rem --- 2) Install PyInstaller + the native-window toolkit ----------------
rem   pywebview + pythonnet give the app a real chrome-less window
rem   (backed by the Edge WebView2 runtime, already built into Windows
rem   10/11) instead of opening a browser tab. pystray + Pillow give it
rem   a system tray icon since there's no console window to interact
rem   with any more.
echo Installing/upgrading PyInstaller and pywebview...
%PY% -m pip install --upgrade pip >nul
%PY% -m pip install --upgrade pyinstaller pywebview pythonnet pystray Pillow
if errorlevel 1 (
    echo [ERROR] Failed to install required Python packages.
    pause
    exit /b 1
)
echo.

rem --- 3) Build the standalone exe with PyInstaller ---------------------
rem   --windowed: no console window at all; the app only shows its own
rem   window + a tray icon, and runs in the background otherwise
rem   (visible in Task Manager as GameForSale.exe).
rem   --paths vendor lets PyInstaller statically see the vendored
rem   "qrcode" package (it's imported dynamically via sys.path, which
rem   PyInstaller can't follow on its own) so it correctly pulls in the
rem   stdlib "xml" modules qrcode's SVG renderer needs. The explicit
rem   --hidden-import flags are a belt-and-braces backup for the same
rem   reason -- without either, the exe builds fine but then crashes at
rem   first run with "ModuleNotFoundError: No module named 'xml'".
rem   --add-data for app_icon.ico bundles it for the tray icon to load.
echo Building GameForSale.exe ...
%PY% -m PyInstaller --noconfirm --onefile --windowed --name GameForSale --icon app_icon.ico --paths vendor --add-data "static;static" --add-data "vendor;vendor" --add-data "catalog.json;." --add-data "app_icon.ico;." --hidden-import xml.etree.ElementTree --hidden-import xml.etree.cElementTree --hidden-import xml.parsers.expat --hidden-import qrcode --hidden-import qrcode.image.svg --hidden-import webview.platforms.edgechromium --hidden-import webview.platforms.winforms --hidden-import webview.platforms.mshtml --hidden-import pystray._win32 --hidden-import PIL._tkinter_finder --collect-all clr_loader --collect-all pythonnet launcher.py
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. See the output above.
    pause
    exit /b 1
)
echo.
echo   -^> dist\GameForSale.exe built successfully.
echo.

rem --- 4) Find or install Inno Setup (ISCC.exe) --------------------------
set "ISCC="
where ISCC >nul 2>nul
if %errorlevel%==0 set "ISCC=ISCC"

if "%ISCC%"=="" if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if "%ISCC%"=="" if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if "%ISCC%"=="" (
    echo Inno Setup not found - trying to install it automatically via winget...
    where winget >nul 2>nul
    if %errorlevel%==0 (
        winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements
    ) else (
        echo [WARN] winget is not available on this computer.
    )
    if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
)

if "%ISCC%"=="" (
    echo.
    echo [NOTE] Could not find or install Inno Setup automatically.
    echo Your app was still built at: dist\GameForSale.exe  (you can already run/share that file).
    echo.
    echo To get a proper "Setup.exe" installer with your icon and a desktop
    echo shortcut, install Inno Setup from https://jrsoftware.org/isdl.php
    echo then run this build.bat again.
    pause
    exit /b 0
)

rem --- 5) Compile the installer ------------------------------------------
echo.
echo Compiling the installer with Inno Setup...
"%ISCC%" installer.iss
if errorlevel 1 (
    echo [ERROR] Inno Setup compilation failed. See the output above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   DONE
echo   Installer: dist_installer\GameForSale-Setup.exe
echo ============================================
pause

@echo off
REM ==========================================================================
REM  social-scout installer for Windows
REM  Installs the Python dependencies needed for the Claude Desktop MCP server.
REM  Double-click this file to run it.
REM ==========================================================================
setlocal
cd /d "%~dp0"

echo.
echo ===========================================================
echo   social-scout  -  installing Python dependencies
echo ===========================================================
echo.

REM --- 1. Check Python is installed ---------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [X] Python was not found on your system.
    echo.
    echo     Please install Python 3.10 or newer from:
    echo         https://www.python.org/downloads/
    echo     IMPORTANT: on the first install screen, tick
    echo     "Add python.exe to PATH", then re-run this installer.
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%v in ('python --version') do echo [OK] Found %%v
echo.

REM --- 2. Install dependencies --------------------------------------------
echo Installing dependencies from requirements.txt ...
echo (this can take a few minutes the first time)
echo.
python -m pip install -r requirements.txt --break-system-packages
if errorlevel 1 (
    echo.
    echo [X] Dependency installation failed. Scroll up to read the error.
    echo     A common fix is to update pip first:
    echo         python -m pip install --upgrade pip
    echo.
    pause
    exit /b 1
)

REM --- 3. Optional: the stealth browser ------------------------------------
echo.
echo -----------------------------------------------------------
echo   Optional: the stealth browser (about 73 MB from GitHub)
echo   Reads JavaScript-heavy pages, takes screenshots, scrapes
echo   many URLs at once, and runs Meta Ad Library / Pinterest /
echo   X profiles without opening Chrome. You can add it later.
echo -----------------------------------------------------------
set "STEALTH="
set /p STEALTH="Install the stealth browser now? [Y/n] "
if /i "%STEALTH%"=="n" goto :skip_stealth
if /i "%STEALTH%"=="no" goto :skip_stealth
python scripts\obscura.py install
if errorlevel 1 (
    echo [!] The stealth browser didn't install - everything else still works.
    echo     Try again later with:  python scripts\obscura.py install
) else (
    echo [OK] Stealth browser installed.
)
:skip_stealth

echo.
echo ===========================================================
echo   [OK] Install complete!
echo ===========================================================
echo.
echo   Next step: connect social-scout to the Claude Desktop App.
echo   Open SETUP_GUIDE.md (in this folder) and follow "Step 3:
echo   Configure Claude Desktop". You will paste a small snippet
echo   that points Claude Desktop at this folder's mcp_server.py.
echo.
echo   This folder is:
echo       %~dp0
echo.
pause
